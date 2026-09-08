"""Guardrail refusals have to outlive the process that refused.

`continuity_agent_rejections_total{reason}` is the series that turns "a
proposal citing a query the model never ran is rejected" from a sentence in a
README into something a reviewer can watch. It was published straight from the
conductor -- a short-lived job whose counter starts at zero, pushes one
increment and exits -- so Prometheus aged it out five minutes later and the
panel went empty.

An empty guardrail panel is not neutral. It says "nothing was ever refused",
which is the single thing this series must never say when it is not true, and
it is indistinguishable from a guardrail that has never fired. Only a durable
record can tell those apart.

So refusals are appended to a file and republished every exporter cycle, the
same shape of answer the autonomy ledger already needed for the same reason.
"""

from __future__ import annotations

import json

import pytest

from agents.ledger import RejectionLog, publish_rejections


class FakeGauge:
    def __init__(self) -> None:
        self.points: list[tuple[float, dict]] = []

    def set(self, value: float, attributes: dict) -> None:
        self.points.append((value, attributes))


class FakeInstruments:
    def __init__(self) -> None:
        self.gauges: dict[str, FakeGauge] = {}

    def gauge(self, name: str, _description: str = "") -> FakeGauge:
        return self.gauges.setdefault(name, FakeGauge())


def test_a_refusal_survives_the_process_that_made_it(tmp_path):
    RejectionLog(tmp_path).append(
        "uncited_evidence", agent="conductor",
        detail="cited dub_sync_offset_ms which was never run", market="de-DE")

    # A completely separate reader, as the exporter is.
    assert [r.reason for r in RejectionLog(tmp_path).entries()] == [
        "uncited_evidence"]


def test_totals_are_counted_by_agent_and_reason(tmp_path):
    log = RejectionLog(tmp_path)
    log.append("uncited_evidence", agent="conductor")
    log.append("uncited_evidence", agent="conductor")
    log.append("unmeasurable_prediction", agent="conductor")
    log.append("uncited_evidence", agent="metadata")

    assert log.totals() == {
        ("conductor", "uncited_evidence"): 2,
        ("conductor", "unmeasurable_prediction"): 1,
        ("metadata", "uncited_evidence"): 1,
    }


def test_republishing_sets_the_total_rather_than_incrementing(tmp_path):
    """The exporter reports a total it read; it did not do the refusing.

    Incrementing would double-count on every cycle, which is how the repair
    ledger went wrong when two publishers both owned the same counter.
    """
    log = RejectionLog(tmp_path)
    log.append("uncited_evidence", agent="conductor")
    log.append("uncited_evidence", agent="conductor")
    instruments = FakeInstruments()

    for _cycle in range(3):
        publish_rejections(instruments, log)

    gauge = instruments.gauges["continuity_agent_rejections_total"]
    assert all(value == 2 for value, _ in gauge.points), (
        "the total must be set from disk, not accumulated per cycle"
    )


def test_an_empty_log_publishes_nothing_rather_than_zero(tmp_path):
    """Absent and zero are different answers, here as everywhere else.

    Publishing `0` would assert that the guardrails ran and refused nothing.
    Publishing nothing says only that there is no record, which is the truth
    before any agent has run.
    """
    instruments = FakeInstruments()
    assert publish_rejections(instruments, RejectionLog(tmp_path)) == 0
    assert instruments.gauges["continuity_agent_rejections_total"].points == []


def test_a_truncated_line_does_not_lose_the_rest_of_the_history(tmp_path):
    """A crash mid-write costs one entry, not the file."""
    log = RejectionLog(tmp_path)
    log.append("uncited_evidence", agent="conductor")
    with log.path.open("a", encoding="utf-8") as fh:
        fh.write('{"reason": "unmeasurab')          # killed mid-write
    log.append("no_evidence", agent="conductor")

    assert [r.reason for r in log.entries()] == [
        "uncited_evidence", "no_evidence"]


def test_detail_is_bounded(tmp_path):
    """The detail is model-supplied text and this file is read every cycle."""
    log = RejectionLog(tmp_path)
    entry = log.append("uncited_evidence", agent="conductor", detail="x" * 5000)
    assert len(entry.detail) == 400


def test_the_conductor_writes_a_refusal_through_genai(tmp_path):
    """End to end: the guardrail path records without being asked to twice.

    `GenAI.rejected` already emitted the counter; the log is attached to the
    same call so a refusal cannot be counted in-process and forgotten on disk.
    """
    from opentelemetry import trace

    from telemetry.genai import GenAI
    from telemetry.metrics import Instruments
    from opentelemetry.metrics import NoOpMeter

    log = RejectionLog(tmp_path)
    genai = GenAI(trace.get_tracer("test"), Instruments(NoOpMeter("test")),
                  "conductor", rejections=log)
    genai.rejected("uncited_evidence", "made_up_query", market="de-DE")

    entries = list(log.entries())
    assert len(entries) == 1
    assert entries[0].reason == "uncited_evidence"
    assert entries[0].market == "de-DE"
    assert entries[0].agent == "conductor"


def test_a_missing_log_never_stops_a_refusal(tmp_path):
    """A guardrail that cannot write its audit line must still refuse.

    Losing the record is bad. Letting a rejected repair through because the
    disk was full would be very much worse, so the write is best-effort and the
    refusal is not.
    """
    from opentelemetry import trace
    from opentelemetry.metrics import NoOpMeter

    from telemetry.genai import GenAI
    from telemetry.metrics import Instruments

    class Broken:
        def append(self, *_args, **_kwargs):
            raise OSError("no space left on device")

    genai = GenAI(trace.get_tracer("test"), Instruments(NoOpMeter("test")),
                  "conductor", rejections=Broken())
    genai.rejected("uncited_evidence", "boom")     # must not raise


def test_the_written_line_is_readable_json(tmp_path):
    """Anyone with a text editor can find out why an agent was refused."""
    log = RejectionLog(tmp_path)
    log.append("unknown_strategy", agent="conductor", detail="RECOLOUR",
               title="SINTEL", market="ja-JP")
    raw = json.loads(log.path.read_text(encoding="utf-8").strip())
    assert raw["reason"] == "unknown_strategy"
    assert raw["market"] == "ja-JP"
    assert raw["at"].endswith("+00:00")
