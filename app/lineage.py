"""OpenLineage run events for the readiness pipeline, written to a JSON-lines file (no lineage server needed).

Jobs (namespace `taskdatareadiness`):
    sap_extract          SAP source tables  -> staging files          (version + row count facets)
    readiness_gate.<t>   staging files      -> (decision)             (data-quality assertion facets per check)
    apply_fixes          source / registry  -> staging files          (one input per fix origin)
    rag_index_build      document files     -> Chroma index
    retrieve.<t>         staging + index    -> agent context

The same events can be sent to Marquez, OpenMetadata or DataHub later by swapping the file transport for HTTP.
"""

from __future__ import annotations

import json
import re
from datetime import UTC
from pathlib import Path

from openlineage.client import OpenLineageClient
from openlineage.client.event_v2 import InputDataset, Job, OutputDataset, Run, RunEvent, RunState
from openlineage.client.facet_v2 import data_quality_assertions_dataset as dqa
from openlineage.client.facet_v2 import dataset_version_dataset as dsv
from openlineage.client.facet_v2 import output_statistics_output_dataset as ost
from openlineage.client.transport.file import FileConfig, FileTransport
from openlineage.client.uuid import generate_new_uuid

NAMESPACE = "taskdatareadiness"
PRODUCER = "https://github.com/local/taskdatareadiness"  # producer URI required by the spec (demo value)


def source_ds(system_name: str, asset: str, version: str | None = None) -> InputDataset:
    facets = {"version": dsv.DatasetVersionDatasetFacet(datasetVersion=version)} if version else {}
    slug = re.sub(r"[^a-z0-9]+", "-", system_name.lower().replace("/", "")).strip("-")  # "S/4HANA MM" -> s4hana-mm
    return InputDataset(namespace=f"sap-sim://{slug}", name=asset, facets=facets)


def staging_in(asset: str, assertions: list[tuple[str, bool]] | None = None) -> InputDataset:
    facets = {}
    if assertions is not None:
        facets["dataQualityAssertions"] = dqa.DataQualityAssertionsDatasetFacet(
            assertions=[dqa.Assertion(assertion=name, success=ok) for name, ok in assertions]
        )
    return InputDataset(namespace="file://staging", name=asset, facets=facets)


def staging_out(asset: str, rows: int | None = None, version: str | None = None) -> OutputDataset:
    facets = {"version": dsv.DatasetVersionDatasetFacet(datasetVersion=version)} if version else {}
    out_facets = {"outputStatistics": ost.OutputStatisticsOutputDatasetFacet(rowCount=rows)} if rows is not None else {}
    return OutputDataset(namespace="file://staging", name=asset, facets=facets, outputFacets=out_facets)


class Lineage:
    def __init__(self, path: Path, clock):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.client = OpenLineageClient(transport=FileTransport(FileConfig(log_file_path=str(self.path), append=True)))

    def run(self, job: str, inputs: list, outputs: list) -> str:
        """Emit START + COMPLETE for one run; returns the run id."""
        run = Run(runId=str(generate_new_uuid()))
        when = self.clock()
        when = (when if when.tzinfo else when.replace(tzinfo=UTC)).isoformat()
        for state in (RunState.START, RunState.COMPLETE):
            self.client.emit(RunEvent(eventType=state, eventTime=when, run=run, job=Job(namespace=NAMESPACE, name=job),
                                      inputs=inputs, outputs=outputs, producer=PRODUCER))  # fmt: skip
        return run.runId

    def events(self, dataset: str | None = None, limit: int = 50) -> list[dict]:
        """COMPLETE events (newest first), optionally only those touching one dataset name."""
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            ev = json.loads(line)
            if ev.get("eventType") != "COMPLETE":
                continue
            names = {d["name"] for d in ev.get("inputs", []) + ev.get("outputs", [])}
            if dataset is None or dataset in names:
                out.append(ev)
        return out[::-1][:limit]

    @staticmethod
    def summary(ev: dict) -> dict:
        return {"job": ev["job"]["name"], "run_id": ev["run"]["runId"], "time": ev["eventTime"],
                "inputs": [f"{d['namespace']}/{d['name']}" for d in ev.get("inputs", [])],
                "outputs": [f"{d['namespace']}/{d['name']}" for d in ev.get("outputs", [])]}  # fmt: skip
