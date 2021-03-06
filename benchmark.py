# -*- coding: utf-8 -*-

"""Run the non-parametric baseline experiment."""

import hashlib
import itertools as itt
import json
import time
from functools import partial
from multiprocessing import cpu_count
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Type, Union, cast

import click
import pandas as pd
from more_click import verbose_option
from pykeen.datasets import Dataset, dataset_resolver
from pykeen.evaluation import RankBasedEvaluator, RankBasedMetricResults
from pykeen.models import Model, baseline
from pykeen.models.baseline import EvaluationOnlyModel
from pykeen.utils import resolve_device
from tqdm import trange
from tqdm.contrib.concurrent import process_map
from tqdm.contrib.logging import logging_redirect_tqdm

HERE = Path(__file__).parent.resolve()
BENCHMARK_DIRECTORY = HERE.joinpath("results")
BENCHMARK_DIRECTORY.mkdir(exist_ok=True, parents=True)
BENCHMARK_PATH = BENCHMARK_DIRECTORY.joinpath("results.tsv")
TEST_BENCHMARK_PATH = BENCHMARK_DIRECTORY.joinpath("test_results.tsv")
RUNS_DIR = HERE.joinpath("runs")
RUNS_DIR.mkdir(exist_ok=True, parents=True)
KS = (1, 5, 10, 50, 100)
METRICS = ["mrr", "iamr", "igmr", *(f"hits@{k}" for k in KS), "aamr", "aamri"]


class Mixin:
    @property
    def device(self):
        return resolve_device("cpu")


class MarginalDistributionBaseline(Mixin, baseline.MarginalDistributionBaseline):
    """A hack to fix the device getters."""


class SoftInverseTripleBaseline(Mixin, baseline.SoftInverseTripleBaseline):
    """A hack to fix the device getters."""


@click.command()
@verbose_option
@click.option("--batch-size", default=2048, show_default=True)
@click.option("--trials", default=10, show_default=True)
@click.option("--rebuild", is_flag=True)
@click.option(
    "--test",
    is_flag=True,
    help="Run on the 5 smallest datasets and output to different path",
)
def main(batch_size: int, trials: int, rebuild: bool, test: bool):
    """Run the baseline showcase."""
    path = TEST_BENCHMARK_PATH if test else BENCHMARK_PATH
    if not path.is_file() or rebuild or test:
        with logging_redirect_tqdm():
            df = _build(batch_size=batch_size, trials=trials, path=path, test=test)
    else:
        df = pd.read_csv(path, sep="\t")

    _plot(df, test=test)


def _melt(df: pd.DataFrame) -> pd.DataFrame:
    keep = [col for col in df.columns if col not in METRICS]
    return pd.melt(
        df[[*keep, *METRICS]],
        id_vars=keep,
        value_vars=METRICS,
        var_name="metric",
    )


def _relabel_model(
    model: str,
    entity_margin: Optional[bool],
    relation_margin: Optional[bool],
    threshold: Optional[float],
) -> str:
    rv = model
    if isinstance(entity_margin, bool):
        rv = f'{rv} {"/e" if entity_margin else ""}'
    if isinstance(relation_margin, bool):
        rv = f'{rv} {"/r" if relation_margin else ""}'
    if threshold and pd.notna(threshold):
        rv = f"{rv} ({threshold})"
    return rv


def _plot(df: pd.DataFrame, skip_small: bool = True, test: bool = False) -> None:
    import matplotlib.pyplot as plt
    import seaborn as sns

    if skip_small and not test:
        df = df[~df.dataset.isin({"Nations", "Countries", "UMLS", "Kinships"})]

    tsdf = _melt(df)
    tsdf["model"] = [
        _relabel_model(model, entity_margin, relation_margin, threshold)
        for model, entity_margin, relation_margin, threshold in tsdf[
            ["model", "entity_margin", "relation_margin", "threshold"]
        ].values
    ]

    # Plot relation between dataset and time, stratified by model
    # Interpretation: exponential relationship between # triples and time
    g = sns.catplot(
        data=tsdf,
        y="dataset",
        x="time",
        hue="model",
        kind="box",
        aspect=1.5,
    ).set(xscale="log", xlabel="Time (seconds)", ylabel="")
    if test:
        times_stub = BENCHMARK_DIRECTORY.joinpath("test_times")
    else:
        times_stub = BENCHMARK_DIRECTORY.joinpath("times")
    g.fig.savefig(times_stub.with_suffix(".svg"))
    g.fig.savefig(times_stub.with_suffix(".png"), dpi=300)
    plt.close(g.fig)

    for metric in ["aamri", "mrr", "iamr"]:
        g = sns.catplot(
            data=tsdf[tsdf.metric == metric],
            y="dataset",
            x="value",
            hue="model",
            kind="bar",
            aspect=1.5,
        ).set(xlabel=metric, ylabel="")
        if test:
            stub = BENCHMARK_DIRECTORY.joinpath(f"test_{metric}")
        else:
            stub = BENCHMARK_DIRECTORY.joinpath(metric)
        g.fig.savefig(stub.with_suffix(".svg"))
        g.fig.savefig(stub.with_suffix(".png"), dpi=300)
        plt.close(g.fig)

    # Make a grid showing relation between # triples and result, stratified by model and metric.
    # Interpretation: no dataset size dependence
    g = sns.catplot(
        data=tsdf[~tsdf.metric.isin({"aamr", "aamri"})],
        y="dataset",
        x="value",
        hue="model",
        col="metric",
        kind="bar",
        sharex=False,
        col_wrap=2,
        height=0.5 * tsdf["dataset"].nunique(),
        aspect=1.5,
    )
    g.set(ylabel="")
    if test:
        summary_stub = BENCHMARK_DIRECTORY.joinpath("test_summary")
    else:
        summary_stub = BENCHMARK_DIRECTORY.joinpath("summary")
    g.fig.savefig(summary_stub.with_suffix(".svg"))
    g.fig.savefig(summary_stub.with_suffix(".png"), dpi=300)
    plt.close(g.fig)


def _get_settings() -> List[Tuple[Type[EvaluationOnlyModel], Mapping[str, Any]]]:
    model_settings: List[Tuple[Type[EvaluationOnlyModel], Mapping[str, Any]]] = []
    for entity_margin, relation_margin in itt.product([True, False], repeat=2):
        model_settings.append(
            (
                MarginalDistributionBaseline,
                dict(entity_margin=entity_margin, relation_margin=relation_margin),
            )
        )
    for threshold in [None, 0.1, 0.3]:
        model_settings.append((SoftInverseTripleBaseline, dict(threshold=threshold)))

    return model_settings


def _build(
    batch_size: int, trials: int, path: Union[str, Path], test: bool = False
) -> pd.DataFrame:
    datasets = sorted(dataset_resolver, key=Dataset.triples_sort_key)
    if test:
        datasets = datasets[:5]
    else:
        # FB15K and CoDEx Large are the first datasets where this gets a bit out of hand
        datasets = datasets[: 1 + datasets.index(dataset_resolver.lookup("FB15k-237"))]

    model_settings = _get_settings()
    kwargs_keys = sorted({key for _, kwargs in model_settings for key in kwargs})
    func = partial(
        _run_trials,
        batch_size=batch_size,
        kwargs_keys=kwargs_keys,
        trials=trials,
    )
    it = process_map(
        func,
        itt.product(datasets, model_settings),
        desc="Baseline",
        total=len(datasets) * len(model_settings),
    )
    rows = list(itt.chain.from_iterable(it))
    columns = [
        "dataset",
        "entities",
        "relations",
        "triples",
        "trial",
        "model",
        *kwargs_keys,
        "time",
        *METRICS,
    ]
    df = pd.DataFrame(rows, columns=columns)
    df.to_csv(path, sep="\t", index=False)
    # print(tabulate(df.round(3).values, headers=columns, tablefmt='github'))
    return df


def _run_trials(
    t: Tuple[Type[Dataset], Tuple[Type[Model], Mapping[str, Any]]],
    *,
    trials: int,
    batch_size: int,
    kwargs_keys: Sequence[str],
) -> List[Tuple[Any, ...]]:
    dataset_cls, (model_cls, model_kwargs) = t

    model_name = model_cls.__name__[: -len("Baseline")]
    dataset_name = dataset_cls.__name__
    kwargs_hash = hashlib.sha256(
        json.dumps(model_kwargs, sort_keys=True).encode("utf-8")
    ).hexdigest()[:8]
    path = RUNS_DIR.joinpath(f"{dataset_name}_{model_name}_{kwargs_hash}.json")
    if path.exists():
        return json.loads(path.read_text())

    dataset = dataset_cls()
    base_record = (
        dataset_name,
        dataset.training.num_entities,
        dataset.training.num_relations,
        dataset.training.num_triples,
    )
    records = []
    for trial in trange(trials, leave=False, desc=f"{dataset_name}/{model_name}"):
        if trials != 0:
            trial_dataset = dataset.remix(random_state=trial)
        else:
            trial_dataset = dataset
        model = model_cls(triples_factory=trial_dataset.training, **model_kwargs)

        start_time = time.time()
        result = _evaluate_baseline(trial_dataset, model, batch_size=batch_size)
        elapsed_seconds = time.time() - start_time

        records.append(
            (
                *base_record,
                trial,
                model_name,
                *(_clean(model_kwargs.get(key)) for key in kwargs_keys),
                elapsed_seconds,
                *(result.get_metric(metric) for metric in METRICS),
            )
        )
    path.write_text(json.dumps(records, indent=2))
    return records


def _clean(x):
    if x is None or pd.isna(x):
        return ""
    return x


def _evaluate_baseline(dataset: Dataset, model: Model, batch_size=None) -> RankBasedMetricResults:
    assert dataset.validation is not None
    evaluator = RankBasedEvaluator(ks=KS)
    return cast(
        RankBasedMetricResults,
        evaluator.evaluate(
            model=model,
            mapped_triples=dataset.testing.mapped_triples,
            batch_size=batch_size,
            additional_filter_triples=[
                dataset.training.mapped_triples,
                dataset.validation.mapped_triples,
            ],
            use_tqdm=100_000 < dataset.training.num_triples,  # only use for big datasets
        ),
    )


if __name__ == "__main__":
    main()
