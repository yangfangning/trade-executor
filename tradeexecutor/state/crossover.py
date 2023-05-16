"""Provides functions to detect crossovers.

- crossover between two series
- crossover between a series and a constant value
"""

import pandas as pd
import numpy as np


epsilon = 1e-10  # This is just an example. You may need to adjust this value.


def has_crossover_occurred(
        series1: pd.Series, 
        series2: pd.Series | int, 
        min_values_above_cross: int | None = 1,
        min_values_below_cross: int | None = 1,
        buffer_above_percent: float | None = 0,
        buffer_below_percent: float | None = 0,
        min_gradient: float | None = None,
        return_cross_index: bool | None = False,
    ) -> bool:
    """Detect if the first series has crossed above the second series/int. Typically usage will be in decide_trades() 
    
    :param series1:
        A pandas.Series object.
        
    :param series2:
        A pandas.Series object.
    
    :param values_above_cross:
        The number of values of series1 directly after the cross that must be above the crossover.

    :param values_below_cross:
        The number of values of series1 directly before the cross that must be below the crossover.

    :param buffer_above_percent:
        The minimum percentage that the series must exceed the crossover value by for the crossover to be considered.
    
    :param buffer_below_percent:
        The minimum percentage difference the series must be below the crossover value by for the crossover to be considered.
    
    :param return_cross_index:
        If True, return the index of the crossover. If False, return True if a crossover has occurred, False otherwise.
        
    :returns:
        bool. True if the series has crossed over the other series in the latest iteration taking values_above_cross and values_below_cross into considertaion, False otherwise.
        
    """

    series1 = _convert_list_to_series(series1)
    series2 = _convert_list_to_series(series2)

    # make the series have the same length
    # trim series2 to the same length as series1 (using latest values)
    if len(series2) > len(series1):
        series2 = series2.iloc[-len(series1):]
    elif len(series1) > len(series2):
        series1 = series1.iloc[-len(series2):]

    _validate_args(
        series1, 
        series2, 
        min_values_above_cross, 
        min_values_below_cross,
        buffer_above_percent,
        buffer_below_percent,
    )

    # find the latest index of crossover
    cross_index = None
    for i,x in enumerate(series1):
        if i == 0:
            continue
        
        if (series1.iloc[i-1] <= series2.iloc[i-1] and x > series2.iloc[i]):
            # cross_index will be first index after the crossover
            cross_index = -(len(series1) - i)

            # cross value if value of second series directly after the crossover
            cross_value = series2.iloc[i]

            # to avoid division by zero
            if cross_value == 0:
                cross_value = epsilon

            # don't break here, we want the latest index of crossover
        

    if not cross_index:
        return _get_return_value(False, return_cross_index, cross_index)

    after_cross1 = series1.iloc[cross_index:]
    
    # buffer_above_percent
    if not (max(after_cross1) - cross_value)/cross_value >= buffer_above_percent:
        return _get_return_value(False, return_cross_index, cross_index)

    # min_values_above_cross
    if len(after_cross1) < min_values_above_cross:
        return _get_return_value(False, return_cross_index, cross_index)
    after_cross1_cut = after_cross1.iloc[:min_values_above_cross]
    if any(x <= cross_value for x in after_cross1_cut):
        return _get_return_value(False, return_cross_index, cross_index)

    before_cross1 = series1.iloc[:cross_index]
    # make sure there are values below the cross
    if not any(x < cross_value for x in before_cross1):
        return _get_return_value(False, return_cross_index, cross_index)

    # min_values_below_cross
    if len(before_cross1) < min_values_below_cross:
        return _get_return_value(False, return_cross_index, cross_index)
    before_cross1_cut = before_cross1.iloc[-min_values_below_cross:]
    if any(x > cross_value for x in before_cross1_cut):
        return _get_return_value(False, return_cross_index, cross_index)
    
    # buffer_below_percent
    if not (cross_value - min(before_cross1))/cross_value >= buffer_below_percent:
        return _get_return_value(False, return_cross_index, cross_index)

    # min_gradient
    if min_gradient is not None:
        x = np.arange(len(series1))
        y = _standardize(series1.values)
        slope, _ = np.polyfit(x, y, 1)
        if slope < min_gradient:
            return _get_return_value(False, return_cross_index, cross_index)

    # If we get here, then all conditions are met

    return _get_return_value(True, return_cross_index, cross_index)


def has_crossunder_occurred(
        series1: pd.Series, 
        series2: pd.Series | int, 
        min_values_above_cross: int | None = 1,
        min_values_below_cross: int | None = 1,
        buffer_above_percent: float | None = 0,
        buffer_below_percent: float | None = 0,
        min_gradient: float | None = None,
        return_cross_index: bool | None = False,
    ) -> bool:
    """Detect if the first series has crossed below the second series/int. 
    
    :param series1:
        A pandas.Series object.
        
    :param series2:
        A pandas.Series object.
    
    :param values_above_cross:
        The number of values of series1 directly before the cross that must be above the crossover.

    :param values_below_cross:
        The number of values of series1 directly after the cross that must be below the crossover.

    :param buffer_above_percent:
        The minimum percentage difference the series must be above the crossover value by for the crossover to be considered.
    
    :param buffer_below_percent:
        The minimum percentage that the series must fall below the crossover value by for the crossover to be considered.
    
    :param return_cross_index:
        If True, return the index of the crossunder. If False, return True if a crossunder has occurred, False otherwise.
        
    :returns:
        bool. True if the series has crossed under the other series in the latest iteration taking values_above_cross and values_below_cross into consideration, False otherwise.
    """
    
    series1 = _convert_list_to_series(series1)
    series2 = _convert_list_to_series(series2)

    # make the series have the same length
    # trim series2 to the same length as series1 (using latest values)
    if len(series2) > len(series1):
        series2 = series2.iloc[-len(series1):]
    elif len(series1) > len(series2):
        series1 = series1.iloc[-len(series2):]

    _validate_args(
        series1, 
        series2, 
        min_values_above_cross, 
        min_values_below_cross,
        buffer_above_percent,
        buffer_below_percent,
    )

    # find the latest index of crossunder
    cross_index = None
    for i,x in enumerate(series1):
        if i == 0:
            continue

        if (series1.iloc[i-1] >= series2.iloc[i-1] and x < series2.iloc[i]):
            cross_index = -(len(series1) - i)

            # cross value is value of second series directly after the crossunder
            cross_value = series2.iloc[i]

            # to avoid division by zero
            if cross_value == 0:
                cross_value = epsilon

    if not cross_index:
        return _get_return_value(False, return_cross_index, cross_index)

    after_cross1 = series1.iloc[cross_index:]
    if len(after_cross1) < min_values_below_cross:
        return _get_return_value(False, return_cross_index, cross_index)
    if not (cross_value - min(after_cross1))/cross_value >= buffer_below_percent:
        return _get_return_value(False, return_cross_index, cross_index)

    after_cross1_cut = after_cross1.iloc[:min_values_below_cross]
    if any(x > cross_value for x in after_cross1_cut):
        return _get_return_value(False, return_cross_index, cross_index)

    before_cross1 = series1.iloc[:cross_index]
    # make sure there are values above the cross
    if not any(x > cross_value for x in before_cross1):
        return False
    if len(series1.iloc[:-cross_index]) < min_values_above_cross:
        return _get_return_value(False, return_cross_index, cross_index)
    if not (max(before_cross1) - cross_value)/cross_value >= buffer_above_percent:
        return _get_return_value(False, return_cross_index, cross_index)

    before_cross1_cut = before_cross1.iloc[-min_values_above_cross:]
    if any(x < cross_value for x in before_cross1_cut):
        return _get_return_value(False, return_cross_index, cross_index)

    # min_gradient
    if min_gradient is not None:
        x = np.arange(len(series1))
        y = _standardize(series1.values)
        slope, _ = np.polyfit(x, y, 1)
        if slope > -min_gradient:
            return _get_return_value(False, return_cross_index, cross_index)

    # If we get here, then all conditions are met

    return _get_return_value(True, return_cross_index, cross_index)


def _get_return_value(
        return_value: bool,
        return_cross_index: bool,
        cross_index: int | None,
    ) -> bool:
    """Return the correct return value based on the value of return_cross_index."""
    
    if return_value == False:
        cross_index = None

    if return_cross_index:
        return return_value, cross_index
    else:
        return return_value


def _convert_list_to_series(series):
    
    if isinstance(series, list):
        return pd.Series(series)
    
    return series


def _validate_args(
    series1,
    series2, 
    min_values_above_cross, 
    min_values_below_cross,
    min_percent_diff1,
    min_percent_diff2,
):
    assert type(series1) == pd.Series, "series1 must be a pandas.Series object"
    assert type(series2) == pd.Series, "series2 must be a pandas.Series object"
    assert type(min_values_above_cross) == int and min_values_above_cross > 0, "min_values_above_cross must be a positive int"
    assert type(min_values_below_cross) == int and min_values_below_cross > 0, "min_values_below_cross must be a positive int"

    assert type(min_percent_diff1) in {float, int} and 0 <= min_percent_diff1 <= 1, "min_percent_above must be a number between 0 and 1"

    assert type(min_percent_diff2) in {float, int} and 0 <= min_percent_diff2 <= 1, "min_percent_below must be a number between 0 and 1"


def _standardize(series):
    mean = series.mean()
    std_dev = series.std()
    standardized_series = (series - mean) / std_dev
    return standardized_series
