from collections import namedtuple
from dataclasses import dataclass
from random import choice, randint, sample
from typing import Tuple

import numpy as np
from pandas.compat._optional import import_optional_dependency

from .pipelines.dynamicworld import DynamicWorld2020_2021
from .pipelines.s1_s2_era5_srtm import (
    BANDS_GROUPS_IDX,
    NORMED_BANDS,
    NUM_TIMESTEPS,
    TIMESTEPS_IDX,
)

MASK_STRATEGIES = (
    "group_bands",
    "random_timesteps",
    "chunk_timesteps",
    "random_combinations",
)


# This is to allow a quick expansion of the mask from
# group-channel space into real-channel space
BAND_EXPANSION = [len(x) for x in BANDS_GROUPS_IDX.values()]
REMAPPED_INDICES = [0] * 17
for group_index, (group, indices) in enumerate(BANDS_GROUPS_IDX.items()):
    up_to = sum(BAND_EXPANSION[:group_index])
    for i, idx in enumerate(indices):
        REMAPPED_INDICES[idx] = up_to + i


MaskedExample = namedtuple(
    "MaskedExample",
    ["mask_eo", "mask_dw", "x_eo", "y_eo", "x_dw", "y_dw", "start_month", "latlon", "strategy"],
)


def make_mask(strategy: str, mask_ratio: float) -> Tuple[np.ndarray, np.ndarray]:
    """
    Make a mask for a given strategy and percentage of masked values.
    Args:
        strategy: The masking strategy to use. One of MASK_STRATEGIES
        mask_ratio: The percentage of values to mask. Between 0 and 1.
    """
    mask = np.full((NUM_TIMESTEPS, len(BANDS_GROUPS_IDX)), False)
    dw_mask = np.full(NUM_TIMESTEPS, False)
    num_tokens_masked = int((NUM_TIMESTEPS * (len(BANDS_GROUPS_IDX) + 1)) * mask_ratio)
    # RANDOM BANDS
    if strategy == "group_bands":
        # tuple because of mypy, which thinks lists can only hold one type
        band_groups: Tuple = tuple(list(range(len(BANDS_GROUPS_IDX)))) + ("DW",)
        num_band_groups_to_mask = int(num_tokens_masked / NUM_TIMESTEPS)
        band_groups_to_mask = sample(band_groups, num_band_groups_to_mask)
        for band_group in band_groups_to_mask:
            if band_group == "DW":
                dw_mask[:] = True
            else:
                mask[:, band_group] = True

    # RANDOM TIMESTEPS
    elif strategy == "random_timesteps":
        timesteps_to_mask = int(num_tokens_masked / (len(BANDS_GROUPS_IDX) + 1))
        timesteps = sample(TIMESTEPS_IDX, k=timesteps_to_mask)
        mask[timesteps] = True
        dw_mask[timesteps] = True
    elif strategy == "chunk_timesteps":
        timesteps_to_mask = int(num_tokens_masked / (len(BANDS_GROUPS_IDX) + 1))
        start_idx = randint(0, NUM_TIMESTEPS - timesteps_to_mask)
        mask[start_idx : start_idx + timesteps_to_mask] = True  # noqa
        dw_mask[start_idx : start_idx + timesteps_to_mask] = True  # noqa

    # RANDOM COMBINATIONS
    elif strategy == "random_combinations":
        all_tokens_mask = np.full(NUM_TIMESTEPS * (len(BANDS_GROUPS_IDX) + 1), False)
        all_tokens_mask[:num_tokens_masked] = True
        np.random.shuffle(all_tokens_mask)
        dw_mask = all_tokens_mask[:NUM_TIMESTEPS]
        mask = all_tokens_mask[NUM_TIMESTEPS:].reshape((NUM_TIMESTEPS, len(BANDS_GROUPS_IDX)))
    else:
        raise ValueError(f"Unknown strategy {strategy} not in {MASK_STRATEGIES}")

    return np.repeat(mask, BAND_EXPANSION, axis=1)[:, REMAPPED_INDICES], dw_mask


@dataclass
class MaskParams:
    strategies: Tuple[str, ...] = ("NDVI",)
    ratio: float = 0.5

    def __post_init__(self):
        for strategy in self.strategies:
            assert strategy in [
                "group_bands",
                "random_timesteps",
                "chunk_timesteps",
                "random_combinations",
            ]
        num_tokens_masked = (NUM_TIMESTEPS * (len(BANDS_GROUPS_IDX) + 1)) * self.ratio
        print(num_tokens_masked)
        assert num_tokens_masked % 1 == 0
        if ("random_timesteps" in self.strategies) or ("chunk_timesteps" in self.strategies):
            assert num_tokens_masked % NUM_TIMESTEPS == 0
        if "group_bands" in self.strategies:
            assert num_tokens_masked % (len(BANDS_GROUPS_IDX) + 1) == 0

    def mask_data(self, eo_data: np.ndarray, dw_data: np.ndarray):
        strategy = choice(self.strategies)
        mask, dw_mask = make_mask(strategy=strategy, mask_ratio=self.ratio)
        x = eo_data * ~mask
        y = np.zeros(eo_data.shape).astype(np.float32)
        y[mask] = eo_data[mask]

        masked_dw_tokens = np.ones_like(dw_data) * DynamicWorld2020_2021.missing_data_class
        x_dw = np.where(dw_mask, masked_dw_tokens, dw_data)
        y_dw = np.zeros(x_dw.shape).astype(np.int16)
        y_dw[dw_mask] = dw_data[dw_mask]

        return mask, dw_mask, x, y, x_dw, y_dw, strategy


def plot_masked_bands(y_true: np.ndarray, y_pred: np.ndarray, mask_strategy: str):
    """Plot only the masked bands over time"""
    ncols = len(BANDS_GROUPS_IDX[mask_strategy])
    plt = import_optional_dependency("matplotlib.pyplot")
    fig, axes = plt.subplots(nrows=1, ncols=ncols, figsize=(20, 10))
    for i, masked_band_idx in enumerate(BANDS_GROUPS_IDX[mask_strategy]):
        if ncols == 1:
            ax = axes
        else:
            ax = axes[i]
        ax.plot(y_true[:, masked_band_idx], label=f"Actual {mask_strategy} band {i}")
        ax.plot(y_pred[:, masked_band_idx], label=f"Prediced {mask_strategy} band {i}")
        ax.set_title(f"{mask_strategy} band {i}")
        ax.set_ylabel(f"{mask_strategy} band {i}")
        ax.set_xlabel("Time interval")
        ax.legend()
    return fig


def plot_masked_general(example: MaskedExample, y_pred: np.ndarray, dw_pred: np.ndarray):
    """Plot all bands over time"""
    plt = import_optional_dependency("matplotlib.pyplot")
    fig, axes = plt.subplots(nrows=7, ncols=5, figsize=(20, 30))

    # Reconstruct eo data
    eo_data_actual = example.x_eo.copy()
    eo_data_actual[example.mask_eo == 1] = example.y_eo[example.mask_eo == 1]
    eo_data_predicted = y_pred

    dw_actual = example.x_dw.copy()
    dw_actual[example.mask_dw == 1] = example.y_dw[example.mask_dw == 1]
    dw_predicted = np.argmax(dw_pred, axis=1)

    row_idx = 0
    for band_group, band_indexes in BANDS_GROUPS_IDX.items():
        if row_idx > 6:
            row_idx = 6
        else:
            col_idx = 0
        for b in band_indexes:
            ax = axes[row_idx, col_idx]
            (pred_line,) = ax.plot(eo_data_predicted[:, b], color="orange")
            (actual_line,) = ax.plot(eo_data_actual[:, b], color="blue")
            ax.set_title(NORMED_BANDS[b])
            ax.set_ylabel(band_group)
            col_idx += 1
        row_idx += 1

    dw_ax = axes[0, 4]
    dw_ax.plot(dw_predicted, color="orange")
    dw_ax.plot(dw_actual, color="blue")
    dw_ax.set_title("Dynamic World")
    dw_ax.set_yticks(list(DynamicWorld2020_2021.legend.keys()))
    dw_ax.set_yticklabels((DynamicWorld2020_2021.legend.values()), rotation=60)

    fig.legend([pred_line, actual_line], ["Predicted", "Actual"], loc="upper left")
    return fig


def plot_masked(example: MaskedExample, eo_pred: np.ndarray, dw_pred: np.ndarray):
    if example.strategy in list(BANDS_GROUPS_IDX.keys()):
        fig = plot_masked_bands(example.y_eo, eo_pred, example.strategy)
    else:
        fig = plot_masked_general(example, eo_pred, dw_pred)
    plt = import_optional_dependency("matplotlib.pyplot")
    plt.suptitle(
        f"Start month: {example.start_month}, "
        + f"Latlon: {example.latlon}"
        + f"\nStrategy: {example.strategy}",
        size=24,
    )
    fig.subplots_adjust(top=0.15)
    fig.tight_layout()
    return fig
