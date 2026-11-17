from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from .contracts import DomainError, duration_seconds, fingerprint, required_text, timestamp


def window(start, end):
    start, end = timestamp(start, "window_start"), timestamp(end, "window_end")
    if start >= end:
        raise DomainError("invalid_window", "window_start must precede window_end")
    return start, end


def ratio(numerator, denominator):
    return str(Decimal(numerator) / Decimal(denominator)) if denominator else None


class CycleAnalytics:
    def __init__(self, store):
        self.store = store

    def report(self, product, start, end):
        product = required_text(product, "product")
        start, end = window(start, end)
        with self.store.read() as db:
            design_rows = [dict(row) for row in db.execute(
                "SELECT design_id,revision,parent_id,released_at,state FROM designs WHERE product=? ORDER BY revision",
                (product,))]
            jobs = [dict(row) for row in db.execute(
                "SELECT j.*,d.revision,od.revision AS opened_revision FROM jobs j"
                " JOIN designs d ON j.effective_design_id=d.design_id"
                " JOIN designs od ON j.design_id=od.design_id"
                " WHERE od.product=? AND j.opened_at>=? AND j.opened_at<? ORDER BY j.opened_at,j.job_id",
                (product, start, end))]
            job_ids = [row["job_id"] for row in jobs]
            steps = defaultdict(list)
            tests = defaultdict(list)
            lots = defaultdict(list)
            source_events = set()
            for job in jobs:
                job_id = job["job_id"]
                for row in db.execute("SELECT * FROM job_steps WHERE job_id=? ORDER BY started_at,stage", (job_id,)):
                    item = dict(row)
                    steps[job_id].append(item)
                    source_events.add(item["event_id"])
                for row in db.execute("SELECT * FROM tests WHERE job_id=? ORDER BY tested_at,test_id", (job_id,)):
                    item = dict(row)
                    tests[job_id].append(item)
                    source_events.add(item["event_id"])
                for row in db.execute(
                    "SELECT r.lot_id,r.quantity,l.part,l.supplier FROM reservations r JOIN lots l ON l.lot_id=r.lot_id WHERE r.job_id=? ORDER BY l.part,r.lot_id",
                    (job_id,)):
                    lots[job_id].append(dict(row))
            events = [dict(row) for row in db.execute(
                "SELECT id,kind,state,error_code,occurred_at,payload FROM events WHERE state!='accepted' ORDER BY id")]
            head = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
            revisions = self._revisions(jobs, steps, tests, lots)
            job_details = [self._job_detail(job, steps[job["job_id"]], tests[job["job_id"]],
                                            lots[job["job_id"]]) for job in jobs]
            impact = self._revision_impact(db, product, jobs)
            quality = self._quality(jobs, steps, tests, events, design_rows, start, end, impact)
            summary = self._summary(jobs, tests, job_details)
            report = {"product": product, "window_start": start, "window_end": end,
                      "source_head": head, "designs": design_rows, "summary": summary,
                      "revisions": revisions, "jobs": job_details, "quality": quality,
                      "revision_impact": impact,
                      "source_event_ids": sorted(source_events)}
            report["fingerprint"] = fingerprint(report)
            return report

    def _job_detail(self, job, steps, tests, lots):
        stages = []
        completed = job["completed_at"]
        for step in steps:
            duration = duration_seconds(step["started_at"], step["completed_at"]) if step["completed_at"] else None
            stages.append({"stage": step["stage"], "started_at": step["started_at"],
                           "completed_at": step["completed_at"], "duration_seconds": duration,
                           "source_event_id": step["event_id"]})
        wait = None
        if stages:
            wait = duration_seconds(job["opened_at"], stages[0]["started_at"])
            for previous, current in zip(stages, stages[1:]):
                if previous["completed_at"]:
                    wait += duration_seconds(previous["completed_at"], current["started_at"])
        total = duration_seconds(job["opened_at"], completed) if completed else None
        test_details = [{"test_id": test["test_id"], "plan_id": test["plan_id"],
                         "tested_at": test["tested_at"], "passed": test["passed"],
                         "failed": test["failed"], "source_event_id": test["event_id"]} for test in tests]
        return {"job_id": job["job_id"], "design_id": job["design_id"],
                "effective_design_id": job["effective_design_id"],
                "revision": job["revision"], "opened_revision": job["opened_revision"],
                "quantity": job["quantity"],
                "status": job["status"], "opened_at": job["opened_at"],
                "completed_at": completed, "parent_job": job["parent_job"],
                "failure_reason": job["failure_reason"], "stages": stages,
                "tests": test_details, "lots": lots, "wait_seconds": wait,
                "cycle_seconds": total}

    def _revisions(self, jobs, steps, tests, lots):
        groups = defaultdict(lambda: {"jobs": 0, "units": 0, "tested": 0, "passed": 0,
                                      "failed": 0, "completed": 0, "scrapped": 0,
                                      "cycle_seconds": [], "stage_seconds": defaultdict(list),
                                      "suppliers": defaultdict(int)})
        for job in jobs:
            group = groups[job["revision"]]
            group["jobs"] += 1
            group["units"] += job["quantity"]
            group["completed"] += job["status"] == "completed"
            group["scrapped"] += job["status"] == "scrapped"
            if job["completed_at"]:
                group["cycle_seconds"].append(duration_seconds(job["opened_at"], job["completed_at"]))
            for step in steps[job["job_id"]]:
                if step["completed_at"]:
                    group["stage_seconds"][step["stage"]].append(
                        duration_seconds(step["started_at"], step["completed_at"]))
            for test in tests[job["job_id"]]:
                group["tested"] += test["passed"] + test["failed"]
                group["passed"] += test["passed"]
                group["failed"] += test["failed"]
            for lot in lots[job["job_id"]]:
                group["suppliers"][lot["supplier"]] += lot["quantity"]
        result = []
        for revision in sorted(groups):
            group = groups[revision]
            durations = group["cycle_seconds"]
            result.append({"revision": revision, "jobs": group["jobs"], "units": group["units"],
                           "tested_units": group["tested"], "passed_units": group["passed"],
                           "failed_units": group["failed"], "pass_rate": ratio(group["passed"], group["tested"]),
                           "completed_jobs": group["completed"], "scrapped_jobs": group["scrapped"],
                           "mean_cycle_seconds": sum(durations) // len(durations) if durations else None,
                           "stage_mean_seconds": {name: sum(values) // len(values) for name, values in sorted(group["stage_seconds"].items())},
                           "supplier_units": dict(sorted(group["suppliers"].items()))})
        return result

    def _revision_impact(self, db, product, jobs):
        job_by_id = {job["job_id"]: job for job in jobs}
        effects = []
        scoped = defaultdict(list)
        for row in db.execute(
            "SELECT e.*,d.revision FROM revision_effects e JOIN designs d ON d.design_id=e.design_id"
            " WHERE d.product=? ORDER BY e.effective_at,e.design_id", (product,)):
            in_window = [item["job_id"] for item in db.execute(
                "SELECT job_id FROM effect_scopes WHERE design_id=? ORDER BY job_id",
                (row["design_id"],)) if item["job_id"] in job_by_id]
            if not in_window:
                continue
            for job_id in in_window:
                scoped[job_id].append(row["design_id"])
            effects.append({"design_id": row["design_id"], "revision": row["revision"],
                            "effective_at": row["effective_at"], "note": row["note"],
                            "source_event_id": row["event_id"], "scope_job_ids": in_window})
        entries = []
        for job_id in sorted(scoped):
            job = job_by_id[job_id]
            history = [dict(item) for item in db.execute(
                "SELECT * FROM dispositions WHERE job_id=? ORDER BY id", (job_id,))]
            active = next((item for item in reversed(history) if item["withdrawn_at"] is None), None)
            if active and active["decision"] == "upgrade":
                classification = "upgraded"
            elif active or job["status"] in ("completed", "failed", "scrapped"):
                classification = "continued"
            else:
                classification = "pending"
            entries.append({
                "job_id": job_id, "status": job["status"],
                "opened_revision": job["opened_revision"], "current_revision": job["revision"],
                "classification": classification, "scopes": sorted(scoped[job_id]),
                "decision": active["decision"] if active else None,
                "decision_design_id": active["design_id"] if active else None,
                "reason": active["reason"] if active else None,
                "decided_at": active["occurred_at"] if active else None,
                "decision_event_id": active["event_id"] if active else None,
                "history": [{"decision": item["decision"], "design_id": item["design_id"],
                             "reason": item["reason"], "occurred_at": item["occurred_at"],
                             "event_id": item["event_id"], "withdrawn_at": item["withdrawn_at"],
                             "withdrawn_event_id": item["withdrawn_event_id"],
                             "withdraw_reason": item["withdraw_reason"]} for item in history]})
        by_job = {entry["job_id"]: entry for entry in entries}
        for effect in effects:
            outcomes = {"upgraded": 0, "continued": 0, "pending": 0}
            for job_id in effect["scope_job_ids"]:
                outcomes[by_job[job_id]["classification"]] += 1
            effect["outcomes"] = outcomes
        return {"effects": effects, "jobs": entries}

    def _quality(self, jobs, steps, tests, events, designs, start, end, impact):
        issues = []
        for job in jobs:
            job_id = job["job_id"]
            if job["status"] in {"open", "in_progress", "awaiting_test"}:
                issues.append({"code": "unfinished_job", "subject": job_id})
            if job["status"] == "awaiting_test" and not tests[job_id]:
                issues.append({"code": "missing_test", "subject": job_id})
            if job["status"] == "completed" and not tests[job_id]:
                issues.append({"code": "unexplained_completion", "subject": job_id})
            if job["status"] == "failed" and not job["failure_reason"]:
                issues.append({"code": "unexplained_failure", "subject": job_id})
            for step in steps[job_id]:
                if step["completed_at"] is None:
                    issues.append({"code": "open_stage", "subject": job_id + "/" + step["stage"]})
        for event in events:
            if start <= event["occurred_at"] < end:
                issues.append({"code": "unresolved_event", "subject": str(event["id"]),
                               "reason": event["error_code"]})
        for entry in impact["jobs"]:
            if entry["classification"] == "pending":
                issues.append({"code": "pending_disposition", "subject": entry["job_id"],
                               "reason": "in revision scope without an active disposition"})
        if not designs:
            issues.append({"code": "unknown_product", "subject": "product"})
        return {"publishable": not issues, "issue_count": len(issues), "issues": issues}

    def _summary(self, jobs, tests, details):
        passed = failed = 0
        for items in tests.values():
            for test in items:
                passed += test["passed"]
                failed += test["failed"]
        completed = [item["cycle_seconds"] for item in details if item["cycle_seconds"] is not None]
        waiting = [item["wait_seconds"] for item in details if item["wait_seconds"] is not None]
        return {"job_count": len(jobs), "unit_count": sum(job["quantity"] for job in jobs),
                "tested_units": passed + failed, "passed_units": passed, "failed_units": failed,
                "pass_rate": ratio(passed, passed + failed),
                "mean_cycle_seconds": sum(completed) // len(completed) if completed else None,
                "mean_wait_seconds": sum(waiting) // len(waiting) if waiting else None,
                "rework_jobs": sum(bool(job["parent_job"]) for job in jobs)}

    def compare(self, product, first_start, first_end, second_start, second_end):
        first = self.report(product, first_start, first_end)
        second = self.report(product, second_start, second_end)
        metrics = ("job_count", "unit_count", "tested_units", "passed_units", "failed_units",
                   "mean_cycle_seconds", "mean_wait_seconds", "rework_jobs")
        delta = {name: second["summary"][name] - first["summary"][name]
                 if second["summary"][name] is not None and first["summary"][name] is not None else None
                 for name in metrics}
        return {"first": first, "second": second, "delta": delta}
