import pandas as pd
from tradeexecutor.state.crossover import crossover, crossunder

def test_crossover_positive():
    series1 = pd.Series([1, 2, 3, 4, 5])
    series2 = pd.Series([8, 7, 6, 5, 4])
    assert crossover(series1, series2) == True

def test_crossover_negative():
    series1 = pd.Series([1, 2, 3, 4, 5])
    series2 = pd.Series([6, 7, 8, 9, 10])
    assert crossover(series1, series2) == False

def test_crossover_touch():
    series1 = pd.Series([1, 2, 3, 2, 1])
    series2 = pd.Series([5, 4, 3, 2, 2])
    assert crossover(series1, series2, lookback_period=5) == False

def test_crossover_multiple():
    series1 = pd.Series([1, 2, 3, 2, 1, 2, 3])
    series2 = pd.Series([3, 2, 1, 2, 3, 2, 1])
    assert crossover(series1, series2, lookback_period=20, must_return_index=True) == (True, -1)

def test_crossover_lookback():
    series1 = pd.Series([1, 2, 3, 2, 3, 2, 3])
    series2 = pd.Series([3, 2, 1, 2, 1, 2, 1])
    assert crossover(series1, series2, lookback_period=2, must_return_index=True) == (False, None)

def test_crossunder_positive():
    series1 = pd.Series([5, 4, 3, 2, 1])
    series2 = pd.Series([-50, -1.23, 0, 1, 2])
    assert crossunder(series1, series2) == True

def test_crossunder_negative():
    series1 = pd.Series([6, 7, 8, 9, 10])
    series2 = pd.Series([1, 2, 3, 4, 5])
    assert crossunder(series1, series2) == False

def test_crossunder_touch():
    series1 = pd.Series([5, 4, 3, 2, 2])
    series2 = pd.Series([1, 2, 3, 2, 1])
    assert crossunder(series1, series2) == False

def test_crossunder_multiple():
    series1 = pd.Series([3, 2, 1, 2, 3, 2, 1])
    series2 = pd.Series([1, 2, 3, 2, 1, 2, 3])
    assert crossunder(series1, series2, lookback_period=4, must_return_index=True) == (True, -1)

def test_crossunder_lookback():
    series1 = pd.Series([3, 2, 1, 2, 1, 2, 1])
    series2 = pd.Series([1, 2, 3, 2, 3, 2, 3])
    assert crossunder(series1, series2, lookback_period=2, must_return_index=True) == (False, None)

