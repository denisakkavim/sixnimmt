"""Outcome metrics, durable-run comparison analysis, and report rendering."""

from sixnimmt.analytics.evaluation import analyse_run
from sixnimmt.analytics.metrics import finish_credits
from sixnimmt.analytics.models import AnalysisSpec, EvaluationReport
from sixnimmt.analytics.reporting import report_markdown, report_terminal

__all__ = ["AnalysisSpec", "EvaluationReport", "analyse_run", "finish_credits", "report_markdown", "report_terminal"]
