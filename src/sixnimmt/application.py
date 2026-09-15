"""One application workflow for fixed lineups and sampled opponent schedules."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event

from sixnimmt.analytics.evaluation import analyse_run
from sixnimmt.analytics.models import EvaluationReport
from sixnimmt.analytics.reporting import report_markdown
from sixnimmt.arena.artifacts import AnalysisArtifactPaths, ArenaRun, publish_analysis
from sixnimmt.arena.execution import run_plan
from sixnimmt.arena.match import ActivityObserver, Observer
from sixnimmt.arena.planning import RunSettings, build_arena_plan
from sixnimmt.arena.players import PlayerConfig


@dataclass(frozen=True)
class RunResult:
    run: ArenaRun
    report: EvaluationReport
    artifacts: AnalysisArtifactPaths | None


def run(
    settings: RunSettings,
    *,
    observer: Observer | None = None,
    on_activity: ActivityObserver | None = None,
    on_progress: Callable[[int], None] | None = None,
    report: Callable[[str], None] | None = None,
    confirm_start: Callable[[], bool] | None = None,
    on_analysis_started: Callable[[], None] | None = None,
    stop_event: Event | None = None,
) -> RunResult:
    """Plan, execute, analyse and publish any run without terminal dependencies."""
    plan = build_arena_plan(settings)
    candidates = {
        candidate.bot if candidate.key is None else candidate.key: candidate for candidate in settings.catalogue
    }
    players = {
        entry.config_id: PlayerConfig(bot=entry.bot, options=candidates[entry.key].options, display_name=entry.label)
        for entry in plan.catalogue
    }
    evidence = run_plan(
        plan,
        output_dir=settings.recording.output_dir,
        trace=settings.recording.trace,
        players=players,
        session=settings.session,
        watch=settings.display.watch,
        observer=observer,
        on_activity=on_activity,
        on_progress=on_progress,
        report=report,
        confirm_start=confirm_start,
        stop_event=stop_event,
    )
    if on_analysis_started is not None:
        on_analysis_started()
    analysis = analyse_run(evidence)
    artifacts = None
    if evidence.artifact_dir is not None:
        artifacts = publish_analysis(evidence.artifact_dir, analysis.model_dump(mode="json"), report_markdown(analysis))
    return RunResult(evidence, analysis, artifacts)
