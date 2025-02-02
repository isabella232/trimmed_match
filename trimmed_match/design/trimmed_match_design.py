# Copyright 2020 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""Library to create a matched pairs design for randomized geo experiment.

Example usage:
  import pandas as pd
  geox_type = GeoXType.HEAVY_UP
  pretest_data = pd.DataFrame(data={
    'date': ['2019-01-01'] * 20 + ['2019-03-01'] * 20,
    'geo': [1, 2] * 20,
    'sales': range(40),
    'cost': range(40),
    'transactions': range(40)})
  response = 'sales'
  spend_proxy = 'cost'
  matching_metrics = {'sales': 1.0, 'transactions': 1.0, 'cost': 0.01}
  time_window_for_design = ['2019-01-01', '2019-03-01']
  time_window_for_eval = ['2019-02-01', '2019-03-01']

  # Create candidate designs
  tmd = trimmed_match_design.TrimmedMatchGeoXDesign(
    geox_type, pretest_data, response, spend_proxy, matching_metrics,
    time_window_for_design, time_window_for_eval)
  budget_list = [1.0, 2.0]
  iroas_list = [0.0, 1.0, 2.0, 3.0]
  num_pairs_filtered_list = range(10)
  candidate_designs = tmd.report_candidate_designs(budget_list, iroas_list,
    num_pairs_filtered_list)
"""

from typing import Dict, List, Optional, Tuple
import warnings

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from trimmed_match.design import common_classes
from trimmed_match.design import geo_assignment
from trimmed_match.design import matched_pairs_rmse
from trimmed_match.design import plot_utilities
from trimmed_match.design import util

# Minimum number of geo pairs.
_DEFAULT_CONFIDENCE_LEVEL = 0.8
_MIN_NUM_PAIRS = 10
# Minimum total spend
_SPEND_TOLERANCE = 1e-10
# Minimum acceptable value for iROAS
_MIN_IROAS = 0

TimeWindow = common_classes.TimeWindow
GeoXType = common_classes.GeoXType
MatchedPairsRMSE = matched_pairs_rmse.MatchedPairsRMSE


class TrimmedMatchGeoXDesign(object):
  """A class to create a randomized geo experimental design using Trimmed Match."""

  def __init__(self,
               geox_type: GeoXType,
               pretest_data: pd.DataFrame,
               time_window_for_design: TimeWindow,
               time_window_for_eval: TimeWindow,
               response: str = 'response',
               spend_proxy: str = 'spend',
               matching_metrics: Optional[Dict[str, float]] = None,
               pairs: Optional[pd.DataFrame] = None):
    """Initializes TrimmedMatchGeoXDesign.

    Args:
      geox_type: str, type of the experiment. See supported values in GeoXType.
      pretest_data: pd.DataFrame (date, geo, ...).
      time_window_for_design: TimeWindow, representing the time period of
        pretest data used for the design (training + eval).
      time_window_for_eval: TimeWindow, representing the time period of pretest
        data used for evaluation of RMSE in estimating iROAS.
      response: str, a column name in pretest_data, by default 'response'.
      spend_proxy: str, a column name in pretest_data, by default 'spend'.
      matching_metrics: dict, mapping a column name to a numeric weight.
      pairs: optional dataframe with columns (geo1, geo2, pair) containing the
        pairs of geos to use for the power analysis.

    Raises:
      ValueError: response or spend_proxy is not in pretest_data.
      ValueError: number of geos in pretest_data is not even.
      ValueError: unable to convert either column response or spend_proxy to
                  numeric.
      ValueError: total spend_proxy is zero
    """
    if response not in pretest_data.columns:
      raise ValueError(f'{response} is not available in pretest_data.')
    if spend_proxy not in pretest_data.columns:
      raise ValueError(f'{spend_proxy} is not available in pretest_data.')
    if pretest_data['geo'].nunique() % 2 != 0:
      raise ValueError(
          f'Number of geos must be even, but got {pretest_data.geo.nunique()}.')
    if pretest_data[spend_proxy].sum() < _SPEND_TOLERANCE:
      raise ValueError(f'The column {spend_proxy} should have some positive ' +
                       f'entries. The sum of {spend_proxy} found is ' +
                       f'{pretest_data[spend_proxy].sum():.2f}')
    if matching_metrics is None:
      matching_metrics = {response: 1.0, spend_proxy: 0.01}

    self._geox_type = geox_type
    self._response = response
    self._spend_proxy = spend_proxy

    self._matching_metrics = matching_metrics.copy()
    if self._response not in self._matching_metrics:
      self._matching_metrics[self._response] = 0.0
    if self._spend_proxy not in self._matching_metrics:
      self._matching_metrics[self._spend_proxy] = 0.0

    self._pretest_data = util.check_input_data(
        pretest_data, list(self._matching_metrics.keys()))
    self._time_window_for_design = time_window_for_design
    self._time_window_for_eval = time_window_for_eval

    if pairs is not None:
      geos_in_pairs = set(pairs['geo1']) | set(pairs['geo2'])
      if not geos_in_pairs.issubset(set(self._pretest_data['geo'])):
        raise ValueError('The geos ' +
                         f'{geos_in_pairs - set(self._pretest_data["geo"])} ' +
                         'appear in the pairs but not in the pretest data.')
      if len(geos_in_pairs) != (len(pairs['geo1']) + len(pairs['geo2'])):
        raise ValueError('Some geos are duplicated in the pairs.')
    # pairs, a dataframe with columns (geo1, geo2, pair) containing the
    # pairs of geos to use for the power analysis.
    self._pairs = pairs
    # geo_level_eval_data, a pd.DataFrame with columns (geo, response,
    # spend, pair) for evaluation of the RMSE.
    self._geo_level_eval_data = None
    self._num_pairs_filtered = 0

  @property
  def pairs(self):
    return self._pairs

  @property
  def geo_level_eval_data(self):
    return self._geo_level_eval_data

  def _create_sign_test_data(self) -> pd.DataFrame:
    """Creates sign test data based on latest pretest data.

    Returns:
      pd.DataFrame with columns (geo, response, spend), where response and
      spend are geo-level overall values for the most recent time period of the
      same duration as self._time_window_for_eval.
    """
    available_dates = self._pretest_data['date'].drop_duplicates().sort_values(
        ascending=False)
    eval_duration = available_dates.between(
        self._time_window_for_eval.first_day,
        self._time_window_for_eval.last_day).sum()
    latest_data = self._pretest_data[self._pretest_data['date'].isin(
        available_dates[:eval_duration])]

    return latest_data.groupby(
        by='geo', as_index=False)[[self._response, self._spend_proxy]].sum()

  def create_geo_pairs(
      self,
      use_cross_validation: bool = True):
    """Creates geo pairs using pretest data and data to evaluate the RMSE.

    Args:
      use_cross_validation: bool, if True then geo pairing uses pretest data
        during time_window_for_design but not during time_window_for_eval,
        otherwise geo pairing uses pretest data during time_window_for_design
        and time_window_for_eval.

    """
    training_and_evaluation = (
        self._pretest_data['date'].between(
            self._time_window_for_design.first_day,
            self._time_window_for_design.last_day)
        | self._pretest_data['date'].between(
            self._time_window_for_eval.first_day,
            self._time_window_for_eval.last_day))
    pretest = self._pretest_data[training_and_evaluation].copy()
    pretest['period'] = (pretest['date'].between(
        self._time_window_for_eval.first_day,
        self._time_window_for_eval.last_day)).astype(int)

    for metric in self._matching_metrics:
      if use_cross_validation:
        pretest['training_' +
                metric] = (1 - pretest['period']) * pretest[metric]
      else:
        pretest['training_' + metric] = pretest[metric]

    for metric in self._matching_metrics:
      pretest['evaluation_' + metric] = pretest[metric] * pretest['period']

    pretest = pretest.groupby('geo', as_index=False).sum()
    pretest['rankscore'] = 0
    for metric, weight in self._matching_metrics.items():
      pretest['rankscore'] += weight * (
          pretest['training_' + metric] / sum(pretest['training_' + metric]))

    geos_ordered = pretest.sort_values(
        ['rankscore', 'geo'], ascending=[False, True]).reset_index(drop=True)
    geopairs_left = geos_ordered.iloc[::2, :].reset_index(drop=True)
    geopairs_right = geos_ordered.iloc[1::2, :].reset_index(drop=True)

    # order by weighted distance between metrics
    dist = 0
    for metric, weight in self._matching_metrics.items():
      dist += weight * (
          abs(geopairs_left['training_' + metric] -
              geopairs_right['training_' + metric]) /
          sum(geos_ordered['training_' + metric]))

    geopairs_left = geopairs_left.assign(dist=dist)
    geopairs_right = geopairs_right.assign(dist=dist)
    pairs = (pd.DataFrame({
        'geo1': geopairs_left['geo'],
        'geo2': geopairs_right['geo'],
        'distance': geopairs_left['dist']
    }).sort_values(by=['distance', 'geo1'],
                   ascending=[False, True])).reset_index(drop=True)
    npairs = geopairs_left.shape[0]
    pairs['pair'] = range(1, npairs + 1)

    self._pairs = pairs

  def create_geo_level_eval_data(self):
    """Creates geo level data to evaluate the RMSE.

    Create geo_level_eval_data, a pd.DataFrame with columns (geo, response,
    spend, pair) for evaluation of the RMSE.
    """
    if self.pairs is None:
      raise ValueError('pairs are not specified.')

    self.pairs.sort_values(by='pair', inplace=True)
    pretest = self._pretest_data[self._pretest_data['date'].between(
        self._time_window_for_eval.first_day,
        self._time_window_for_eval.last_day)].groupby(
            'geo', as_index=False).sum()

    geo_to_pair = pd.DataFrame({
        'geo': self.pairs['geo1'].tolist() + self.pairs['geo2'].tolist(),
        'pair': self.pairs['pair'].tolist() + self.pairs['pair'].tolist()
    })

    geo_level_eval_data = pd.merge(
        pretest[['geo', self._response, self._spend_proxy]],
        geo_to_pair,
        on='geo').rename(columns={
            self._response: 'response',
            self._spend_proxy: 'spend'
        }).sort_values(by=['pair', 'geo'])

    self._geo_level_eval_data = geo_level_eval_data

  def report_candidate_designs(
      self,
      budget_list: List[float],
      iroas_list: List[float],
      num_pairs_filtered_list: List[int],
      use_cross_validation: bool = True,
      num_simulations=200,
      max_trim_rate=0.10
  ) -> Tuple[pd.DataFrame, Dict[Tuple[float, float, int], pd.DataFrame]]:
    """Report the RMSE of iROAS estimate and summary for each candidate design.

    Args:
      budget_list: list of floats.
      iroas_list: list of nonnegative floats.
      num_pairs_filtered_list: list of int, used to filter pairs up to each
        element of num_pairs_filter.
      use_cross_validation: bool, same as in create_geo_pairs().
      num_simulations: int, num of simulations for RMSE evaluation.
      max_trim_rate: float, the argument for estimator.TrimmedMatch; a small
        value implies the need of less trimming, i.e. high quality pairs.

    Returns:
      results: pd.DataFrame, with columns (num_pairs_filtered,
        experiment_response, experiment_spend, spend_response_ratio, budget,
        iroas, rmse, proportion_cost_in_experiment), where experiment_response
        and experiment_spend are the total response and total spend,
        respectively, for both treatment and control during the eval time
        window, and spend_response_ratio is the ratio of experiment_spend to
        experiment_response. Therefore, for hold-back (e.g. LC) or
        go-dark experiments, the cost/baseline ratio for the treatment group is
        equal to spend_response_ratio * 2.
      detailed_results: dict with keys (budget, iroas, num_pairs_filtered) and
        values pd.DataFrames with the results of each simulation. The
        pd.DataFrames have columns (simulation, estimate, trim_rate, std_error,
        conf_interval_low, conf_interval_up, ci_level).

    Raises:
      ValueError if any element in iroas_list is negative.
    """
    if self.pairs is None:
      self.create_geo_pairs(use_cross_validation)

    self.create_geo_level_eval_data()

    max_num_pairs_to_filter = len(self.pairs.index) - _MIN_NUM_PAIRS
    if max_num_pairs_to_filter < 0:
      raise ValueError(
          'the number of geos to design an experiment must be >= ' +
          f'{_MIN_NUM_PAIRS * 2}, got {len(self.pairs.index) * 2}')

    if max(num_pairs_filtered_list) > max_num_pairs_to_filter:
      not_recommended_filters = [
          filter for filter in num_pairs_filtered_list
          if filter > max_num_pairs_to_filter
      ]
      warnings.warn('We will not attempt to filter ' +
                    f'{not_recommended_filters} pairs as we recommend to have' +
                    f' at least {_MIN_NUM_PAIRS} pairs in the design.')
      num_pairs_filtered_list = [
          filter for filter in num_pairs_filtered_list
          if filter <= max_num_pairs_to_filter
      ]
    if min(iroas_list) < _MIN_IROAS:
      invalid_iroas = [iroas for iroas in iroas_list if iroas < _MIN_IROAS]
      raise ValueError('All elements in iroas_list must have non-negative ' +
                       f'values, got {invalid_iroas}.')

    total_spend = self.geo_level_eval_data['spend'].sum()
    results = []
    detailed_results = {}
    for iroas in iroas_list:
      for budget in budget_list:
        for num_pairs_filtered in num_pairs_filtered_list:

          unfiltered = self.geo_level_eval_data['pair'] > num_pairs_filtered
          geo_level_eval_data_unfiltered = self.geo_level_eval_data.loc[
              unfiltered, :]

          matched_rmse_class = MatchedPairsRMSE(self._geox_type,
                                                geo_level_eval_data_unfiltered,
                                                budget, iroas)
          (expected_rmse, detailed_simulations) = matched_rmse_class.report(
              num_simulations, max_trim_rate)

          experiment_response = geo_level_eval_data_unfiltered['response'].sum()
          experiment_spend = geo_level_eval_data_unfiltered['spend'].sum()
          spend_response_ratio = experiment_spend / experiment_response

          proportion_cost_in_experiment = experiment_spend / total_spend

          results.append({
              'num_pairs_filtered':
                  num_pairs_filtered,
              'experiment_response':
                  experiment_response,
              'experiment_spend':
                  experiment_spend,
              'spend_response_ratio':
                  spend_response_ratio,
              'budget':
                  budget,
              'iroas':
                  iroas,
              'rmse':
                  expected_rmse,
              'proportion_cost_in_experiment':
                  proportion_cost_in_experiment,
              'rmse_cost_adjusted':
                  expected_rmse / proportion_cost_in_experiment,
          })
          detailed_results[(budget, iroas,
                            num_pairs_filtered)] = detailed_simulations

    results = pd.DataFrame(results)
    return (results, detailed_results)

  def plot_candidate_design(
      self, results: pd.DataFrame) -> Dict[Tuple[float, float], plt.Axes]:
    """Plot the RMSE curve for a set of candidate designs.

    Args:
      results: pd.DataFrame, with columns (num_pairs_filtered,
        experiment_response, experiment_spend, spend_response_ratio, budget,
        iroas, rmse, proportion_cost_in_experiment). Results can be the output
        of the method report_candidate_designs.

    Returns:
      axes_dict: a dictionary with keys (budget, iroas) with the plot of the
        RMSE values as a function of the number of excluded pairs for the design
        with corresponding budget and iROAS.
    """
    axes_dict = plot_utilities.plot_candidate_design_rmse(
        self._response, len(self._pairs.index), results)

    return axes_dict

  def output_chosen_design(
      self,
      num_pairs_filtered: int,
      base_seed: int,
      confidence: float = _DEFAULT_CONFIDENCE_LEVEL,
      group_control: int = common_classes.GeoAssignment.CONTROL,
      group_treatment: int = common_classes.GeoAssignment.TREATMENT
  ) -> np.ndarray:
    """Plot the comparison between treatment and control of a candidate design.

    Args:
      num_pairs_filtered: int, number of pairs to filter from the experiment.
      base_seed: seed for the random number generator.
      confidence: float in (0, 1), confidence level for 2-sided CI.
      group_control: value representing the control group in the data.
      group_treatment: value representing the treatment group in the data.

    Returns:
      an array of subplots containing the scatterplot and time series comparison
        for the response and spend of the two groups.
    """
    self.generate_balanced_assignment(
        num_pairs_filtered=num_pairs_filtered,
        base_seed=base_seed,
        confidence=confidence,
        group_control=group_control,
        group_treatment=group_treatment)

    return plot_utilities.output_chosen_design(
        self._pretest_data, self._geo_level_eval_data, self._response,
        self._spend_proxy, self._num_pairs_filtered, self._time_window_for_eval,
        group_control, group_treatment)

  def generate_balanced_assignment(
      self,
      num_pairs_filtered: int,
      base_seed: int,
      confidence: float = _DEFAULT_CONFIDENCE_LEVEL,
      group_control: int = common_classes.GeoAssignment.CONTROL,
      group_treatment: int = common_classes.GeoAssignment.TREATMENT
  ):
    """Generate balanced assignment for the chosen candidate design.

    Args:
      num_pairs_filtered: int, number of pairs to filter from the experiment.
      base_seed: seed for the random number generator.
      confidence: float in (0, 1), confidence level for 2-sided CI.
      group_control: value representing the control group in the data.
      group_treatment: value representing the treatment group in the data.
    """
    self._num_pairs_filtered = num_pairs_filtered
    sign_data = self._create_sign_test_data().rename(columns={
        self._response: 'response',
        self._spend_proxy: 'spend'
    })
    sign_test_data = pd.merge(
        sign_data,
        self._geo_level_eval_data[['geo', 'pair']],
        on='geo', how='outer').fillna({'response': 0, 'spend': 0})
    aa_test_data = self._geo_level_eval_data.copy()

    np.random.seed(base_seed)
    assignment = geo_assignment.generate_balanced_random_assignment(
        sign_test_data[sign_test_data['pair'] > num_pairs_filtered],
        aa_test_data[aa_test_data['pair'] > num_pairs_filtered], confidence,
        confidence)
    self._geo_level_eval_data = self._geo_level_eval_data[[
        'geo', 'pair', 'response', 'spend'
    ]].merge(
        assignment, on=['geo', 'pair'], how='left')

    self._geo_level_eval_data['assignment'] = self._geo_level_eval_data[
        'assignment'].map({
            False: group_control,
            True: group_treatment
        })

  def plot_pair_by_pair_comparison(
      self,
      group_control: int = common_classes.GeoAssignment.CONTROL,
      group_treatment: int = common_classes.GeoAssignment.TREATMENT
  ) -> sns.FacetGrid:
    """Plot the time series of the response variable for each pair.

    Args:
      group_control: value representing the control group in the data.
      group_treatment: value representing the treatment group in the data.

    Returns:
      g: sns.FacetGrid containing one axis for each pair of geos. Each axis
        contains the time series plot of the response variable for the
        treated geo vs the control geo for a particular pair in the design.
    """
    g = plot_utilities.plot_paired_comparison(
        self._pretest_data, self._geo_level_eval_data, self._response,
        self._num_pairs_filtered, self._time_window_for_design,
        self._time_window_for_eval, group_control, group_treatment)

    return g
