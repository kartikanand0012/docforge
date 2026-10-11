"""`PIPELINE_FACTORY=docforge.loadtest.pipelines:build_capacity_pipelines` (with
`SIMULATED_MODEL=true`): the real isolated parser and, through the worker, the real
converter, around a simulated model. Hostile files meet the code that would read them; the
model's time is set by `SIMULATED_MODEL_TIME`."""

from docforge.config import Settings
from docforge.documents import Pipeline
from docforge.loadtest.simulated import SimulatedProvider
from docforge.wiring import build_parser, pipelines_for


def build_capacity_pipelines(settings: Settings) -> dict[str, Pipeline]:
    provider = SimulatedProvider(settings.simulated_model_time)
    return pipelines_for(build_parser(settings), provider, settings)
