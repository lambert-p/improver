# (C) Crown Copyright, Met Office. All rights reserved.
#
# This file is part of 'IMPROVER' and is released under the BSD 3-Clause license.
# See LICENSE in the root of the repository for full licensing details.
"""Provides support utilities for cube manipulation."""

import warnings
from typing import Any, Dict, List, Optional, Union

import iris
import numpy as np
from iris.coords import DimCoord
from iris.cube import Cube, CubeList
from iris.exceptions import CoordinateNotFoundError

from improver import BasePlugin
from improver.metadata.constants import FLOAT_DTYPE, FLOAT_TYPES
from improver.metadata.probabilistic import find_threshold_coordinate
from improver.utilities.common_input_handle import as_cube
from improver.utilities.cube_checker import check_cube_coordinates


def collapsed(cube: Cube, *args: Any, **kwargs: Any) -> Cube:
    """Collapses the cube with given arguments.

    The cell methods of the output cube will match the cell methods
    from the input cube. Any cell methods generated by the iris
    collapsed method will not be retained.

    Args:
        cube:
            A Cube to be collapsed.

    Returns:
        A collapsed cube where the cell methods match the input cube.
    """
    original_methods = cube.cell_methods
    new_cube = cube.collapsed(*args, **kwargs)
    new_cube.cell_methods = original_methods

    # demote escalated datatypes as required
    if new_cube.dtype in FLOAT_TYPES:
        new_cube.data = new_cube.data.astype(FLOAT_DTYPE)

    collapsed_coords = args[0] if isinstance(args[0], list) else [args[0]]
    for coord in collapsed_coords:
        if new_cube.coord(coord).points.dtype in FLOAT_TYPES:
            new_cube.coord(coord).points = new_cube.coord(coord).points.astype(
                FLOAT_DTYPE
            )
            if new_cube.coord(coord).bounds is not None:
                new_cube.coord(coord).bounds = new_cube.coord(coord).bounds.astype(
                    FLOAT_DTYPE
                )

    return new_cube


def collapse_realizations(cube: Cube, method="mean") -> Cube:
    """Collapses the realization coord of a cube and strips the coord from the cube.

    Args:
        cube:
            Cube to be aggregated.
        method:
            One of "sum", "mean", "median", "std_dev", "min", "max";
            default is "mean".
    Returns:
        Cube with realization coord collapsed and removed.
    """

    aggregator_dict = {
        "sum": iris.analysis.SUM,
        "mean": iris.analysis.MEAN,
        "median": iris.analysis.MEDIAN,
        "std_dev": iris.analysis.STD_DEV,
        "min": iris.analysis.MIN,
        "max": iris.analysis.MAX,
    }

    aggregator = aggregator_dict.get(method)
    if aggregator is None:
        raise ValueError(f"method must be one of {list(aggregator_dict.keys())}")

    returned_cube = collapsed(cube, "realization", aggregator)
    returned_cube.remove_coord("realization")

    if (
        (method == "std_dev")
        and (len(cube.coord("realization").points) == 1)
        and (np.ma.is_masked(returned_cube.data))
    ):
        # Standard deviation is undefined. Iris masks the entire output,
        # but we also set the underlying data to np.nan here.
        returned_cube.data.data[:] = np.nan

    return returned_cube


def get_dim_coord_names(cube: Cube) -> List[str]:
    """
    Returns an ordered list of dimension coordinate names on the cube

    Args:
        cube

    Returns:
        List of dimension coordinate names
    """
    return [coord.name() for coord in cube.coords(dim_coords=True)]


def get_coord_names(cube: Cube) -> List[str]:
    """
    Returns a list of all coordinate names on the cube

    Args:
        cube

    Returns:
        List of all coordinate names
    """
    return [coord.name() for coord in cube.coords()]


def strip_var_names(cubes: Union[Cube, CubeList]) -> CubeList:
    """
    Strips var_name from the cube and from all coordinates except where
    required to support probabilistic metadata.  Inputs are modified in place.

    Args:
        cubes

    Returns:
        cubes with stripped var_name
    """
    if isinstance(cubes, iris.cube.Cube):
        cubes = iris.cube.CubeList([cubes])
    for cube in cubes:
        cube.var_name = None
        for coord in cube.coords():
            # retain var name required for threshold coordinate
            if coord.var_name != "threshold":
                coord.var_name = None
    return cubes


class MergeCubes(BasePlugin):
    """
    Class adding functionality to iris.merge_cubes()

    Accounts for differences in attributes, cell methods and bounds ranges to
    avoid merge failures and anonymous dimensions.
    """

    def __init__(self) -> None:
        """Initialise constants"""
        # List of attributes to remove silently if unmatched
        self.silent_attributes = ["history", "title", "mosg__grid_version"]

    @staticmethod
    def _equalise_cell_methods(cubelist: CubeList) -> None:
        """
        Function to equalise cell methods that do not match.  Modifies cubes
        in place.

        Args:
            cubelist:
                List of cubes to check the cell methods and revise.
        """
        cell_methods = cubelist[0].cell_methods
        for cube in cubelist[1:]:
            cell_methods = list(set(cell_methods) & set(cube.cell_methods))
        for cube in cubelist:
            cube.cell_methods = tuple(cell_methods)

    @staticmethod
    def _check_time_bounds_ranges(cube: Cube) -> None:
        """
        Check the bounds on any dimensional time coordinates after merging.
        For example, to check time and forecast period ranges for accumulations
        to avoid blending 1 hr with 3 hr accumulations.  If points on the
        coordinate are not compatible, raise an error.

        Args:
            cube:
                Merged cube
        """
        for name in ["time", "forecast_period"]:
            try:
                coord = cube.coord(name)
            except CoordinateNotFoundError:
                continue

            if coord.bounds is None:
                continue
            if len(coord.points) == 1:
                continue

            bounds_ranges = np.abs(np.diff(coord.bounds))
            reference_range = bounds_ranges[0]
            if not np.all(np.isclose(bounds_ranges, reference_range)):
                msg = (
                    "Cube with mismatching {} bounds ranges "
                    "cannot be blended".format(name)
                )
                raise ValueError(msg)

    def process(
        self,
        cubes_in: Union[List[Cube], CubeList],
        check_time_bounds_ranges: bool = False,
        slice_over_realization: bool = False,
        copy: bool = True,
    ) -> Cube:
        """
        Function to merge cubes, accounting for differences in attributes,
        coordinates and cell methods.  Note that cubes with different sets
        of coordinates (as opposed to cubes with the same coordinates with
        different values) cannot be merged.

        If the input is a single Cube, this is returned unmodified.  A
        CubeList of length 1 is checked for mismatched time bounds before
        returning the single Cube (since a CubeList of this form may be the
        result of premature iris merging on load).

        Args:
            cubes_in:
                Cubes to be merged.
            check_time_bounds_ranges:
                Flag to check whether scalar time bounds ranges match.
                This is for when we are expecting to create a new "time" axis
                through merging for eg precipitation accumulations, where we
                want to make sure that the bounds match so that we are not eg
                combining 1 hour with 3 hour accumulations.
            slice_over_realization:
                Options to combine cubes with different realization dimensions.
                These cannot always be concatenated directly as this can create a
                non-monotonic realization coordinate.
            copy:
                If True, this will copy the cubes, thus not having any impact on
                the original objects.

        Returns:
            Merged cube.
        """
        # if input is already a single cube, return unchanged
        if isinstance(cubes_in, iris.cube.Cube):
            return cubes_in

        if len(cubes_in) == 1:
            # iris merges cubelist into shortest list possible on load
            # - may already have collapsed across invalid time bounds
            if check_time_bounds_ranges:
                self._check_time_bounds_ranges(cubes_in[0])
            return cubes_in[0]

        if copy:
            # create copies of input cubes so as not to modify in place
            cube_return = lambda cube: cube.copy()
        else:
            cube_return = lambda cube: cube

        cubelist = iris.cube.CubeList([])
        for cube in cubes_in:
            if slice_over_realization:
                for real_slice in cube.slices_over("realization"):
                    cubelist.append(cube_return(real_slice))
            else:
                cubelist.append(cube_return(cube))

        # equalise cube attributes, cell methods and coordinate names
        iris.util.equalise_attributes(cubelist)
        strip_var_names(cubelist)
        self._equalise_cell_methods(cubelist)

        # merge resulting cubelist
        result = cubelist.merge_cube()

        # check time bounds if required
        if check_time_bounds_ranges:
            self._check_time_bounds_ranges(result)

        return result


def get_filtered_attributes(cube: Cube, attribute_filter: Optional[str] = None) -> Dict:
    """
    Build dictionary of attributes that match the attribute_filter. If the
    attribute_filter is None, return all attributes.

    Args:
        cube:
            A cube from which attributes partially matching the
            attribute_filter will be returned.
        attribute_filter:
            A string to match, or partially match, against attributes to build
            a filtered attribute dictionary. If None, all attributes are
            returned.
    Returns:
        A dictionary of attributes partially matching the attribute_filter
        that were found on the input cube.
    """
    attributes = cube.attributes
    if attribute_filter is not None:
        attributes = {k: v for (k, v) in attributes.items() if attribute_filter in k}
    return attributes


def compare_attributes(
    cubes: CubeList, attribute_filter: Optional[str] = None
) -> List[Dict]:
    """
    Function to compare attributes of cubes

    Args:
        cubes:
            List of cubes to compare (must be more than 1)
        attribute_filter:
            A string to filter which attributes are actually compared. If None
            all attributes are compared.

    Returns:
        List of dictionaries of unmatching attributes

    Warns:
        Warning: If only a single cube is supplied
    """
    unmatching_attributes = []
    if len(cubes) == 1:
        msg = "Only a single cube so no differences will be found "
        warnings.warn(msg)
    else:
        reference_attributes = get_filtered_attributes(
            cubes[0], attribute_filter=attribute_filter
        )

        common_keys = reference_attributes.keys()
        for cube in cubes[1:]:
            cube_attributes = get_filtered_attributes(
                cube, attribute_filter=attribute_filter
            )
            common_keys = {
                key
                for key in cube_attributes.keys()
                if key in common_keys
                and np.all(cube_attributes[key] == reference_attributes[key])
            }

        for cube in cubes:
            cube_attributes = get_filtered_attributes(
                cube, attribute_filter=attribute_filter
            )
            unique_attributes = {
                key: value
                for (key, value) in cube_attributes.items()
                if key not in common_keys
            }
            unmatching_attributes.append(unique_attributes)

    return unmatching_attributes


def compare_coords(
    cubes: CubeList, ignored_coords: Optional[List[str]] = None
) -> List[Dict]:
    """
    Function to compare the coordinates of the cubes

    Args:
        cubes:
            List of cubes to compare (must be more than 1)
        ignored_coords:
            List of coordinate names that identify coordinates to exclude from
            the comparison.

    Returns:
        List of dictionaries of unmatching coordinates
        Number of dictionaries equals number of cubes
        unless cubes is a single cube in which case
        unmatching_coords returns an empty list.

    Warns:
        Warning: If only a single cube is supplied
    """
    if ignored_coords is None:
        ignored_coords = []

    unmatching_coords = []
    if len(cubes) == 1:
        msg = "Only a single cube so no differences will be found "
        warnings.warn(msg)
    else:
        common_coords = cubes[0].coords()
        for cube in cubes[1:]:
            cube_coords = cube.coords()
            common_coords = [
                coord
                for coord in common_coords
                if (
                    coord in cube_coords
                    and np.all(cube.coords(coord) == cubes[0].coords(coord))
                )
            ]

        for i, cube in enumerate(cubes):
            unmatching_coords.append({})
            for coord in cube.coords():
                if coord not in common_coords and coord.name() not in ignored_coords:
                    dim_coords = cube.dim_coords
                    if coord in dim_coords:
                        dim_val = dim_coords.index(coord)
                    else:
                        dim_val = None
                    aux_val = None
                    if dim_val is None and len(cube.coord_dims(coord)) > 0:
                        aux_val = cube.coord_dims(coord)[0]
                    unmatching_coords[i].update(
                        {
                            coord.name(): {
                                "data_dims": dim_val,
                                "aux_dims": aux_val,
                                "coord": coord,
                            }
                        }
                    )

    return unmatching_coords


def sort_coord_in_cube(cube: Cube, coord: str, descending: bool = False) -> Cube:
    """Sort a cube based on the ordering within the chosen coordinate.
    Sorting can either be in ascending or descending order.
    This code is based upon https://gist.github.com/pelson/9763057.

    Args:
        cube:
            The input cube to be sorted.
        coord:
            Name of the coordinate to be sorted.
        descending:
            If True it will be sorted in descending order.

    Returns:
        Cube where the chosen coordinate has been sorted into either
        ascending or descending order.

    Warns:
        Warning if the coordinate being processed is a circular coordinate.
    """
    coord_to_sort = cube.coord(coord)
    if isinstance(coord_to_sort, DimCoord):
        if coord_to_sort.circular:
            msg = (
                "The {} coordinate is circular. If the values in the "
                "coordinate span a boundary then the sorting may return "
                "an undesirable result.".format(coord_to_sort.name())
            )
            warnings.warn(msg)
    (dim,) = cube.coord_dims(coord_to_sort)
    index = [slice(None)] * cube.ndim
    index[dim] = np.argsort(coord_to_sort.points)
    if descending:
        index[dim] = index[dim][::-1]
    return cube[tuple(index)]


def enforce_coordinate_ordering(
    cube: Cube, coord_names: Union[List[str], str], anchor_start: bool = True
) -> None:
    """
    Function to reorder dimensions within a cube.
    Note that the input cube is modified in place.

    Args:
        cube:
            Cube where the ordering will be enforced to match the order within
            the coord_names. This input cube will be modified as part of this
            function.
        coord_names:
            List of the names of the coordinates to order. If a string is
            passed in, only the single specified coordinate is reordered.
        anchor_start:
            Define whether the specified coordinates should be moved to the
            start (True) or end (False) of the list of dimensions. If True, the
            coordinates are inserted as the first dimensions in the order in
            which they are provided. If False, the coordinates are moved to the
            end. For example, if the specified coordinate names are
            ["time", "realization"] then "realization" will be the last
            coordinate within the cube, whilst "time" will be the last but one.
    """
    if isinstance(coord_names, str):
        coord_names = [coord_names]

    # construct a list of dimensions on the cube to be reordered
    dim_coord_names = get_dim_coord_names(cube)
    coords_to_reorder = []
    for coord in coord_names:
        if coord == "threshold":
            try:
                coord = find_threshold_coordinate(cube).name()
            except CoordinateNotFoundError:
                continue
        if coord in dim_coord_names:
            coords_to_reorder.append(coord)

    original_coords = cube.coords(dim_coords=True)
    coord_dims = cube.coord_dims

    # construct list of reordered dimensions assuming start anchor
    new_dims = [coord_dims(coord)[0] for coord in coords_to_reorder]
    new_dims.extend(
        [
            coord_dims(coord)[0]
            for coord in original_coords
            if coord_dims(coord)[0] not in new_dims
        ]
    )

    # if anchor is end, reshuffle the list
    if not anchor_start:
        new_dims_end = new_dims[len(coords_to_reorder) :]
        new_dims_end.extend(new_dims[: len(coords_to_reorder)])
        new_dims = new_dims_end

    # transpose cube using new coordinate order
    if new_dims != sorted(new_dims):
        cube.transpose(new_dims)


def clip_cube_data(cube: Cube, minimum_value: float, maximum_value: float) -> Cube:
    """Apply np.clip to data in a cube to ensure that the limits do not go
    beyond the provided minimum and maximum values.

    Args:
        cube:
            The cube that has been processed and contains data that is to be
            clipped.
        minimum_value:
            The minimum value, with data in the cube that falls below this
            threshold set to it.
        maximum_value:
            The maximum value, with data in the cube that falls above this
            threshold set to it.

    Returns:
        The processed cube with the data clipped to the limits of the
        original preprocessed cube.
    """
    original_attributes = cube.attributes
    original_methods = cube.cell_methods

    result = iris.cube.CubeList()
    for cube_slice in cube.slices([cube.coord(axis="y"), cube.coord(axis="x")]):
        cube_slice.data = np.clip(cube_slice.data, minimum_value, maximum_value)
        result.append(cube_slice)

    result = result.merge_cube()
    result.cell_methods = original_methods
    result.attributes = original_attributes
    result = check_cube_coordinates(cube, result)
    return result


def expand_bounds(
    result_cube: Cube, cubelist: Union[List[Cube], CubeList], coord_names: List[str]
) -> Cube:
    """Alter a coordinate on result_cube such that bounds are expanded to cover
    the entire range of the input cubes (cubelist).  The input result_cube is
    modified in place and returned.

    For example, in the case of time cubes if the input cubes have
    bounds of [0000Z, 0100Z] & [0100Z, 0200Z] then the output cube will
    have bounds of [0000Z,0200Z]. The returned coordinate point will be
    equal to the upper bound.

    Args:
        result_cube:
            Cube with coords requiring expansion
        cubelist:
            List of input cubes with source coords
        coord_names:
            Coordinates which should be expanded

    Returns:
        Cube with coords expanded.
    """
    for coord in coord_names:
        if len(result_cube.coord(coord).points) != 1:
            emsg = (
                "the expand bounds function should only be used on a"
                'coordinate with a single point. The coordinate "{}" '
                "has {} points."
            )
            raise ValueError(emsg.format(coord, len(result_cube.coord(coord).points)))

        bounds = [cube.coord(coord).bounds for cube in cubelist]
        if any(b is None for b in bounds):
            if not all(b is None for b in bounds):
                raise ValueError(
                    "cannot expand bounds for a mixture of "
                    "bounded / unbounded coordinates"
                )
            points = [cube.coord(coord).points for cube in cubelist]
            new_low_bound = np.min(points)
            new_top_bound = np.max(points)
        else:
            new_low_bound = np.min(bounds)
            new_top_bound = np.max(bounds)
        result_coord = result_cube.coord(coord)
        result_coord.bounds = np.array([[new_low_bound, new_top_bound]])
        if result_coord.bounds.dtype in FLOAT_TYPES:
            result_coord.bounds = result_coord.bounds.astype(FLOAT_DTYPE)

        result_coord.points = [new_top_bound]
        if result_coord.points.dtype in FLOAT_TYPES:
            result_coord.points = result_coord.points.astype(FLOAT_DTYPE)

    return result_cube


def filter_realizations(cubes: CubeList) -> Cube:
    """For a given list of cubes, identifies the set of times, filters out any realizations
    that are not present at all times and returns a merged cube of the result.

    Args:
        cubes:
            List of cubes to be filtered

    Returns:
        Cube:
            Filtered and merged cube

    """
    times = set()
    realizations = set()
    for cube in cubes:
        times.update([c.point for c in cube.coord("time").cells()])
        realizations.update(cube.coord("realization").points)
    filtered_cubes = CubeList()
    for realization in realizations:
        realization_cube = MergeCubes()(
            cubes.extract(iris.Constraint(realization=realization))
        )
        if set([c.point for c in realization_cube.coord("time").cells()]) == times:
            filtered_cubes.append(realization_cube)
    return MergeCubes()(filtered_cubes)


def add_coordinate_to_cube(
    cube: Cube,
    new_coord: DimCoord,
    new_dim_location: int = 0,
    copy_metadata: bool = True,
) -> Cube:
    """Create a copy of input cube with an additional dimension coordinate
    added to the cube at the specified axis. The data from input cube is broadcast
    over this new dimension.

    Args:
        cube:
            cube to add realization dimension to.
        new_coord:
            new coordinate to add to input cube.
        new_dim_location:
            position in cube.data to position the new dimension coord. Default is
            to add the new coordinate as the leading dimension.
        copy_metadata:
            flag as to whether to carry metadata over to output cube.

    Returns:
        A copy of cube broadcast over the new dimension coordinate.
    """
    input_dim_count = len(cube.dim_coords)

    if (new_dim_location > input_dim_count) or (new_dim_location < 0):
        raise ValueError(
            f"New dimension location: {new_dim_location} incompatible \
                with cube containing {input_dim_count}."
        )

    new_dim_coords = list(cube.dim_coords) + [new_coord]
    new_dims = list(range(input_dim_count + 1))
    new_dim_coords_and_dims = list(zip(new_dim_coords, new_dims))

    aux_coords = cube.aux_coords
    aux_coord_dims = [cube.coord_dims(coord.name()) for coord in aux_coords]
    new_aux_coords_and_dims = list(zip(aux_coords, aux_coord_dims))

    new_coord_size = len(new_coord.points)
    new_data = np.broadcast_to(
        cube.data[..., np.newaxis], shape=cube.shape + (new_coord_size,)
    ).astype(cube.data.dtype)
    output_cube = Cube(
        new_data,
        dim_coords_and_dims=new_dim_coords_and_dims,
        aux_coords_and_dims=new_aux_coords_and_dims,
    )
    if copy_metadata:
        output_cube.metadata = cube.metadata

    final_dim_order = np.insert(
        np.arange(input_dim_count), new_dim_location, values=input_dim_count
    )
    output_cube.transpose(final_dim_order)

    return output_cube


def maximum_in_height(
    cube: Cube,
    lower_height_bound: float = None,
    upper_height_bound: float = None,
    new_name: str = None,
) -> Cube:
    """Calculate the maximum value over the height coordinate. If bounds are specified
    then the maximum value between the lower_height_bound and upper_height_bound is calculated.

    If either the upper or lower bound is None then no bound is applied. For example if no
    lower bound is provided but an upper bound of 300m is provided then the maximum is
    calculated for all vertical levels less than 300m.

    Args:
        cube:
            A cube with a height coordinate.
        lower_height_bound:
            The lower bound for the height coordinate. This is either a float or None if no
            lower bound is desired. Any specified bounds should have the same units as the
            height coordinate of cube.
        upper_height_bound:
            The upper bound for the height coordinate. This is either a float or None if no
            upper bound is desired. Any specified bounds should have the same units as the
            height coordinate of cube.
        new_name:
            The new name to be assigned to the output cube. If unspecified the name of the original
            cube is used.
    Returns:
        A cube of the maximum value over the height coordinate or maximum value between the desired
        height values. This cube inherits Iris' meta-data updates to the height coordinate and to
        the cell methods.

    Raises:
        ValueError:
            If the cube has no vertical levels between the lower_height_bound and upper_height_bound
    """
    cube = as_cube(cube)
    vertical_levels = cube.coord("height").points

    # replace None in bounds with a numerical value either below or above the range of height
    # levels in the cube so it can be used as a constraint.
    if lower_height_bound is None:
        lower_height_bound = min(vertical_levels)
    if upper_height_bound is None:
        upper_height_bound = max(vertical_levels)

    height_constraint = iris.Constraint(
        height=lambda height: lower_height_bound <= height <= upper_height_bound
    )
    cube_subsetted = cube.extract(height_constraint)

    if cube_subsetted is None:
        raise ValueError(
            f"""The provided cube doesn't have any vertical levels between the provided bounds.
                         The provided bounds were {lower_height_bound},{upper_height_bound}."""
        )

    if len(cube_subsetted.coord("height").points) > 1:
        max_cube = cube_subsetted.collapsed("height", iris.analysis.MAX)
    else:
        max_cube = cube_subsetted

    if new_name:
        max_cube.rename(new_name)

    return max_cube


def height_of_maximum(
    cube: Cube, max_cube: Cube, find_lowest: bool = True, new_name: str = None
) -> Cube:
    """Calculates the vertical level at which the maximum value has been calculated. This
    takes in a cube with values at different heights, and also a cube with the maximum
    of these heights. It compares these (default is to start at the lowest height and
    work down through the vertical levels), and then outputs the height it reaches the
    maximum value.

    Args:
        cube:
            A cube with a height coordinate.
        max_cube:
            A cube of the maximum value over the height coordinate.
        find_lowest:
            If true then the lowest maximum height will be found (for cases where
            there are two heights with the maximum vertical velocity.) Otherwise the highest
            height will be found.
        new_name:
            The new name to be assigned to the output cube. If unspecified the name of the
            original cube is used.
    Returns:
        A cube of heights at which the maximum values occur.

    Raises:
        ValueError:
            If the cube has only 1 vertical level or if an input other than high or low is
            tried for the high_or_low value.
    """
    height_of_max = max_cube.copy()
    height_range = range(len(cube.coord("height").points))
    if len(cube.coord("height").points) == 1:
        raise ValueError("More than 1 vertical level is required.")
    if find_lowest:
        height_points = height_range
    else:
        height_points = reversed(height_range)

    for height in height_points:
        height_of_max.data = np.where(
            cube[height].data == max_cube.data,
            cube[height].coord("height").points[0],
            height_of_max.data,
        )
    if new_name:
        height_of_max.rename(new_name)
    height_of_max.units = cube.coord("height").units
    return height_of_max


def manipulate_n_realizations(cube: Cube, n_realizations: int) -> Cube:
    """Extend or reduce the number of realizations in a cube.

    If more realizations are requested than are in the input cube, then the ensemble
    realizations are recycled. If fewer realizations are requested than are in the input
    cube, then only the first n ensemble realizations are used.

    Args:
        cube: a cube with a realization dimension
        n_realizations: the number of realizations in the output cube

    Returns:
        A cube containing a number of realizations equal to n_realizations.

    Raises:
        ValueError: input cube does not contain realizations
    """
    if not cube.coords("realization", dim_coords=True):
        input_coords = [c.name() for c in cube.coords(dim_coords=True)]
        msg = (
            "Input cube does not contain realizations. The following dimension "
            f"coordinates were found: {input_coords}"
        )
        raise ValueError(msg)
    elif len(cube.coord("realization").points) == n_realizations:
        output = cube.copy()
    else:
        raw_forecast_realizations_extended = iris.cube.CubeList()
        realization_list = []
        mpoints = cube.coord("realization").points
        # Loop over the number of output realizations and find the
        # corresponding ensemble realization number. The ensemble
        # realization numbers are recycled e.g. 1, 2, 3, 1, 2, 3, etc.
        for index in range(n_realizations):
            realization_list.append(mpoints[index % len(mpoints)])

        # Assume that the ensemble realizations are ascending linearly from a given
        # value.
        new_realization_numbers = realization_list[0] + list(range(n_realizations))

        # Extract the realizations required in the realization_list from
        # the input cube. Edit the realization number as appropriate and
        # append to a cubelist containing rebadged raw ensemble realizations.
        for realization, index in zip(realization_list, new_realization_numbers):
            constr = iris.Constraint(realization=realization)
            raw_forecast_realization = cube.extract(constr)
            raw_forecast_realization.coord("realization").points = index
            raw_forecast_realizations_extended.append(raw_forecast_realization)

        output = MergeCubes()(
            raw_forecast_realizations_extended, slice_over_realization=True
        )

    return output
