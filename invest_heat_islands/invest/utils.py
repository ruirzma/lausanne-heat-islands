import os
import tempfile
from collections import abc
from os import path

import dask
import numpy as np
import numpy.random as rn
import pandas as pd
import rasterio as rio
import salem  # noqa: F401
import xarray as xr
from natcap.invest import urban_cooling_model as ucm
from rasterio import transform
from sklearn import metrics

# constants useful for the invest ucm
DEFAULT_MODEL_PARAMS = {
    't_air_average_radius': 500,
    'green_area_cooling_distance': 100,
    'cc_weight_shade': 0.6,
    'cc_weight_albedo': 0.2,
    'cc_weight_eti': 0.2
}
DEFAULT_EXTRA_UCM_ARGS = {'do_valuation': False, 'cc_method': 'factors'}


# simulated annealing
def compute_accept_prob(cost, cost_new, T, rescaling_coeff=2):
    if cost_new < cost:
        # accept probability is 1 since the new cost is lower
        return 1
    else:
        # probabilities
        p = np.exp(-rescaling_coeff * (cost_new - cost) / T)
        return p


def compute_T(fraction):
    return max(0.01, min(1, 1 - fraction))


class GetNeighbour(object):
    def __init__(self, stepsize=0.3):
        self.stepsize = stepsize

    def __call__(self, x):
        s = self.stepsize
        x_neighbour = []
        for x_k in x:
            x_neighbour.append(x_k * (1 + rn.uniform(-s, s)))
        weight_sum = sum(x_neighbour[2:])
        for k in range(2, 5):
            x_neighbour[k] /= weight_sum

        return x_neighbour


def simulated_annealing(x0,
                        cost_func,
                        get_neighbour_func,
                        accept_prob_func=compute_accept_prob,
                        T_func=compute_T,
                        cost_func_args=None,
                        accept_prob_func_args=None,
                        num_iters=100,
                        verbose=True,
                        print_func=None):
    if cost_func_args is None:
        cost_func_args = []
    if accept_prob_func_args is None:
        accept_prob_func_args = []
    if print_func is None:
        print_func = print
    # adapted from https://bit.ly/2QZyKTM
    x = x0
    cost = cost_func(x, *cost_func_args)
    states, costs = [x], [cost]
    for i in range(1, num_iters + 1):
        fraction = i / float(num_iters)
        T = T_func(fraction)
        i_msg = f"Iteration #{i:>2}/{num_iters:>2}: T={T:>4.3g}"
        x_new = get_neighbour_func(x)
        try:
            cost_new = cost_func(x_new, *cost_func_args)
        except ValueError:
            # sometimes InVEST finds nan's that can throw a `ValueError`
            # x = x_new
            if verbose:
                print_func(i_msg +
                           f", skipping x={x} due to a ValueError raised by "
                           "InVEST urban cooling model")
            continue
        i_msg += f", x'={x_new}, cost'={cost_new:>4.3g}"
        if accept_prob_func(cost, cost_new, T,
                            *accept_prob_func_args) > rn.random():
            i_msg += ", accepting x'"
            x, cost = x_new, cost_new

            states.append(x)
            costs.append(cost)
            # accept it
        # else:
        #     reject it
        if verbose:
            print_func(i_msg)

    return states, costs


# utils for the invest ucm
def _get_ref_eto_filepath(date, dst_dir):
    return path.join(
        dst_dir, f'ref_eto_{pd.to_datetime(date).strftime("%Y-%m-%d")}.tif')


def dump_ref_et_rasters(ref_et_da, dst_dir):
    # ref_et_da = xr.open_dataarray(ref_et_filepath)

    # prepare metadata to dump the potential evapotranspiration rasters
    ref_et_da.name = 'ref_et'
    grid = ref_et_da.salem.grid
    width = grid.nx
    height = grid.ny
    west, east, south, north = grid.extent
    # prepare metadata to dump the potential evapotranspiration rasters
    meta = dict(driver='GTiff',
                dtype=ref_et_da.dtype,
                nodata=np.nan,
                width=width,
                height=height,
                count=1,
                transform=transform.from_bounds(west, south, east, north,
                                                width, height),
                crs=ref_et_da.attrs['pyproj_srs'])

    ref_et_filepath_dict = {}
    for date, ref_et_day_da in ref_et_da.groupby('time'):
        ref_et_filepath = _get_ref_eto_filepath(date, dst_dir)
        with rio.open(ref_et_filepath, 'w', **meta) as dst:
            dst.write(ref_et_day_da.values, 1)
        ref_et_filepath_dict[pd.to_datetime(date)] = ref_et_filepath

    return ref_et_filepath_dict


# wrapper to calibrate InVEST's urban cooling model
class ModelWrapper:
    def __init__(self,
                 lulc_raster_filepath,
                 biophysical_table_filepath,
                 ref_et_rasters,
                 aoi_vector_filepath,
                 station_tair_filepath,
                 station_locations_filepath,
                 workspace_dir=None,
                 model_params=None,
                 extra_ucm_args=None,
                 num_workers=8):
        # model parameters
        self.base_args = {
            'lulc_raster_path': lulc_raster_filepath,
            'biophysical_table_path': biophysical_table_filepath,
            'aoi_vector_path': aoi_vector_filepath,
        }
        if model_params is None:
            model_params = DEFAULT_MODEL_PARAMS
        self.base_args.update(**model_params)
        if extra_ucm_args is None:
            extra_ucm_args = DEFAULT_EXTRA_UCM_ARGS
        self.base_args.update(**extra_ucm_args)

        if workspace_dir is None:
            # TODO: how do we ensure that this is removed?
            workspace_dir = tempfile.mkdtemp()
            # TODO: log to warn that we are using a temporary directory
        self.workspace_dir = workspace_dir

        # evapotranspiration rasters for each date
        if isinstance(ref_et_rasters, abc.Mapping):
            # this is a mapping of the form day->ref et filepath
            ref_et_filepath_dict = ref_et_rasters
        else:  # data array or nc file
            ref_et_dir = path.join(workspace_dir, 'ref_et')
            if not path.exists(ref_et_dir):
                os.mkdir(ref_et_dir)
            if not isinstance(ref_et_rasters, xr.DataArray):
                # str, file object or pathlib.Path
                ref_et_rasters = xr.open_dataarray(ref_et_rasters)
            # dump them so that we have a dict of the form day->ref et filepath
            ref_et_filepath_dict = dump_ref_et_rasters(ref_et_rasters,
                                                       ref_et_dir)
        self.ref_et_filepath_dict = ref_et_filepath_dict

        # for the calibration
        station_location_df = pd.read_csv(station_locations_filepath,
                                          index_col=0)
        with rio.open(lulc_raster_filepath) as src:
            self.station_rows, self.station_cols = transform.rowcol(
                src.transform, station_location_df['x'],
                station_location_df['y'])
            # useful to predict air temperature rasters
            self.meta = src.meta.copy()
            self.data_mask = src.dataset_mask().astype(bool)

        station_tair_df = pd.read_csv(station_tair_filepath,
                                      index_col=0)[station_location_df.index]
        station_tair_df.index = pd.to_datetime(station_tair_df.index)
        self.dates = station_tair_df.index
        self.station_tair_df = station_tair_df

        # tref and uhi max
        self.Tref_ser = station_tair_df.min(axis=1)
        self.uhi_max_ser = station_tair_df.max(axis=1) - self.Tref_ser

        # prepare the flat observation array to compute mean squared error
        obs_arr = station_tair_df.values.flatten()
        self.obs_mask = ~np.isnan(obs_arr)
        self.obs_arr = obs_arr[self.obs_mask]

        # number of workers to perform each calibration iteration at scale
        if num_workers is None:
            num_workers = min(len(self.ref_et_filepath_dict), os.cpu_count())
        self.num_workers = num_workers

    @property
    def grid_x(self):
        try:
            return self._grid_x
        except AttributeError:
            cols = np.arange(self.meta['width'])
            x, _ = transform.xy(self.meta['transform'], cols, cols)
            self._grid_x = x
            return self._grid_x

    @property
    def grid_y(self):
        try:
            return self._grid_y
        except AttributeError:
            rows = np.arange(self.meta['height'])
            _, y = transform.xy(self.meta['transform'], rows, rows)
            self._grid_y = y
            return self._grid_y

    def predict_T_arr(self, date, model_args=None, read_kws=None):
        if model_args is None:
            model_args = self.base_args
        if read_kws is None:
            read_kws = {}

        # note that this workspace_dir corresponds to this date only
        workspace_dir = path.join(self.workspace_dir, str(date))
        date_args = model_args.copy()
        date_args.update(
            workspace_dir=workspace_dir,
            ref_eto_raster_path=self.ref_et_filepath_dict[date],
            # t_ref=Tref_da.sel(time=date).item(),
            # uhi_max=uhi_max_da.sel(time=date).item()
            t_ref=self.Tref_ser[date],
            uhi_max=self.uhi_max_ser[date])
        ucm.execute(date_args)

        with rio.open(path.join(workspace_dir, 'intermediate',
                                'T_air.tif')) as src:
            return src.read(1, **read_kws)

    def predict_T_stations(self, date, model_args=None):
        return self.predict_T_arr(date, model_args,
                                  {})[self.station_rows, self.station_cols]

    def predict_T_da(self, dates=None, read_kws=None):
        if dates is None:
            # just so that we can iterate over the dictionary keys (dates)
            dates = list(self.ref_et_filepath_dict.keys())

        if len(dates) == 1:
            T_arrs = [self.predict_T_arr(dates[0], self.base_args, read_kws)]
        else:
            pred_delayed = [
                dask.delayed(self.predict_T_arr)(date, self.base_args,
                                                 read_kws) for date in dates
            ]

            T_arrs = list(
                dask.compute(*pred_delayed,
                             scheduler='processes',
                             num_workers=self.num_workers))
        T_da = xr.DataArray(T_arrs,
                            dims=('time', 'y', 'x'),
                            coords={
                                'time': dates,
                                'y': self.grid_y,
                                'x': self.grid_x
                            },
                            name='T',
                            attrs={'pyproj_srs': self.meta['crs'].to_proj4()})
        return T_da.groupby(
            'time').apply(lambda x: x.where(self.data_mask, np.nan))

    def _params_to_rmse(self, x):
        iter_args = self.base_args.copy()
        iter_args.update(t_air_average_radius=x[0],
                         green_area_cooling_distance=x[1],
                         cc_weight_shade=x[2],
                         cc_weight_albedo=x[3],
                         cc_weight_eti=x[4])

        # we could also iterate over the index of `Tref_ser` or `uhi_max_ser`
        pred_delayed = [
            dask.delayed(self.predict_T_stations)(date, iter_args)
            for date in self.ref_et_filepath_dict
        ]

        preds = dask.compute(*pred_delayed,
                             scheduler='processes',
                             num_workers=self.num_workers)

        return metrics.mean_squared_error(self.obs_arr,
                                          np.hstack(preds)[self.obs_mask])

    def calibrate(self,
                  x0=None,
                  stepsize=0.3,
                  accept_coeff=2,
                  num_iters=100,
                  print_func=None):
        if x0 is None:
            x0 = [
                self.base_args[param_key] for param_key in DEFAULT_MODEL_PARAMS
            ]
        get_neighbour = GetNeighbour(stepsize)
        states, rmses = simulated_annealing(
            x0,
            self._params_to_rmse,
            get_neighbour,
            accept_prob_func_args=[accept_coeff],
            num_iters=num_iters,
            print_func=print_func)
        # get the calibrated parameters and update the model parameters
        x_opt = states[np.argmin(rmses)]
        self.base_args.update({
            param_key: x_param
            for param_key, x_param in zip(DEFAULT_MODEL_PARAMS, x_opt)
        })

        return states, rmses

    def get_comparison_df(self):
        tair_pred_df = pd.DataFrame(index=self.station_tair_df.columns)

        T_da = self.predict_T_da()
        for date, date_da in T_da.groupby('time'):
            tair_pred_df[date] = date_da.values[self.station_rows, self.
                                                station_cols]
        tair_pred_df = tair_pred_df.transpose()

        # comparison_df['err'] = comparison_df['pred'] - comparison_df['obs']
        # comparison_df['sq_err'] = comparison_df['err']**2
        return pd.concat([self.station_tair_df.stack(),
                          tair_pred_df.stack()],
                         axis=1).reset_index().rename(columns={
                             'level_0': 'date',
                             'level_1': 'station',
                             0: 'obs',
                             1: 'pred'
                         })
