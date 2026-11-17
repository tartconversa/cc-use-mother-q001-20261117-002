from .contracts import DomainError, MissingDependency, keys, required_text


DECISIONS = ("continue", "upgrade")
TERMINAL = ("completed", "failed", "scrapped")


class RevisionRules:
    """登记设计修订的生效范围与在制工单处置决定。

    处置投影只增不改:dispositions 行记录决定与撤回,作业的有效设计列
    (jobs.effective_design_id)随决定/撤回切换,原 design_id、领料与
    加工血缘保持不变。
    """

    def __init__(self, db):
        self.db = db

    def effect(self, payload, occurred_at, event_id):
        keys(payload, {"design_id", "jobs"}, {"design_id", "jobs", "note"})
        design_id = required_text(payload["design_id"], "design_id")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise DomainError("invalid_note", "note must be short text")
        job_ids = payload["jobs"]
        if not isinstance(job_ids, list) or not job_ids:
            raise DomainError("invalid_scope", "jobs must be a nonempty list")
        scope = []
        for item in job_ids:
            job_id = required_text(item, "job_id")
            if job_id in scope:
                raise DomainError("duplicate_job", "scope lists a job twice")
            scope.append(job_id)
        design = self.db.execute("SELECT * FROM designs WHERE design_id=?",
                                 (design_id,)).fetchone()
        if design is None:
            raise MissingDependency("design", design_id)
        if design["state"] != "released":
            raise DomainError("retired_design", "cannot register effect for a retired design")
        if occurred_at < design["released_at"]:
            raise DomainError("time_order", "effect precedes design release")
        existing = self.db.execute("SELECT design_id FROM revision_effects WHERE design_id=?",
                                   (design_id,)).fetchone()
        if existing:
            raise DomainError("duplicate_effect", "revision effect already registered")
        for job_id in scope:
            job = self._job_at_effective(job_id)
            if job is None:
                raise MissingDependency("job", job_id)
            if job["product"] != design["product"]:
                raise DomainError("invalid_scope", f"job {job_id} belongs to another product")
            if job["revision"] >= design["revision"]:
                raise DomainError("invalid_scope",
                                  f"job {job_id} is already at or beyond revision {design['revision']}")
            if job["opened_at"] > occurred_at:
                raise DomainError("invalid_scope", f"job {job_id} opened after the effect time")
            if job["status"] in TERMINAL:
                raise DomainError("terminal_job", f"job {job_id} is no longer in progress")
        self.db.execute(
            "INSERT INTO revision_effects(design_id,effective_at,note,event_id) VALUES(?,?,?,?)",
            (design_id, occurred_at, note, event_id))
        self.db.executemany("INSERT INTO effect_scopes(design_id,job_id) VALUES(?,?)",
                            [(design_id, job_id) for job_id in scope])
        return {"design_id": design_id, "effective_at": occurred_at, "jobs": scope}

    def disposition(self, payload, occurred_at, event_id):
        keys(payload, {"job_id", "design_id", "decision", "reason"})
        job_id = required_text(payload["job_id"], "job_id")
        design_id = required_text(payload["design_id"], "design_id")
        decision = required_text(payload["decision"], "decision")
        if decision not in DECISIONS:
            raise DomainError("invalid_decision", f"decision must be one of {', '.join(DECISIONS)}")
        reason = required_text(payload["reason"], "reason")
        job = self._job_at_effective(job_id)
        if job is None:
            raise MissingDependency("job", job_id)
        design = self.db.execute("SELECT * FROM designs WHERE design_id=?",
                                 (design_id,)).fetchone()
        if design is None:
            raise MissingDependency("design", design_id)
        effect = self.db.execute("SELECT * FROM revision_effects WHERE design_id=?",
                                 (design_id,)).fetchone()
        if effect is None:
            raise MissingDependency("revision effect", design_id)
        scoped = self.db.execute("SELECT job_id FROM effect_scopes WHERE design_id=? AND job_id=?",
                                 (design_id, job_id)).fetchone()
        if scoped is None:
            raise MissingDependency("revision effect scope", f"{design_id}:{job_id}")
        if job["status"] in TERMINAL:
            raise DomainError("terminal_job", "terminal job cannot be dispositioned")
        if occurred_at < effect["effective_at"]:
            raise DomainError("time_order", "disposition precedes revision effect")
        if self.active(job_id) is not None:
            raise DomainError("disposition_exists",
                              "job already has an active disposition; withdraw it first", 409)
        if decision == "upgrade":
            if design["state"] != "released":
                raise DomainError("retired_design", "cannot upgrade onto a retired design")
            if design["product"] != job["product"] or design["revision"] <= job["revision"]:
                raise DomainError("invalid_upgrade",
                                  "upgrade target must be a later revision of the same product")
        self.db.execute(
            "INSERT INTO dispositions(job_id,design_id,decision,reason,occurred_at,event_id)"
            " VALUES(?,?,?,?,?,?)",
            (job_id, design_id, decision, reason, occurred_at, event_id))
        if decision == "upgrade":
            self.db.execute("UPDATE jobs SET effective_design_id=? WHERE job_id=?",
                            (design_id, job_id))
        return {"job_id": job_id, "decision": decision, "design_id": design_id,
                "effective_design_id": design_id if decision == "upgrade" else job["effective_design_id"]}

    def withdraw(self, payload, occurred_at, event_id):
        keys(payload, {"job_id"}, {"job_id", "reason"})
        job_id = required_text(payload["job_id"], "job_id")
        reason = payload.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 500:
            raise DomainError("invalid_note", "reason must be short text")
        job = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            raise MissingDependency("job", job_id)
        active = self.active(job_id)
        if active is None:
            raise MissingDependency("active disposition", job_id)
        if job["status"] in TERMINAL:
            raise DomainError("terminal_job", "disposition of a terminal job cannot be withdrawn")
        if occurred_at < active["occurred_at"]:
            raise DomainError("time_order", "withdrawal precedes disposition")
        self.db.execute(
            "UPDATE dispositions SET withdrawn_at=?,withdrawn_event_id=?,withdraw_reason=? WHERE id=?",
            (occurred_at, event_id, reason, active["id"]))
        if active["decision"] == "upgrade":
            self.db.execute("UPDATE jobs SET effective_design_id=design_id WHERE job_id=?", (job_id,))
        return {"job_id": job_id, "withdrawn_decision": active["decision"],
                "design_id": active["design_id"], "effective_design_id": job["design_id"]}

    def active(self, job_id):
        row = self.db.execute(
            "SELECT * FROM dispositions WHERE job_id=? AND withdrawn_at IS NULL ORDER BY id DESC LIMIT 1",
            (job_id,)).fetchone()
        return dict(row) if row is not None else None

    def for_job(self, job_id):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM dispositions WHERE job_id=? ORDER BY id", (job_id,))]

    def get_effect(self, design_id):
        design_id = required_text(design_id, "design_id")
        row = self.db.execute(
            "SELECT e.*,d.product,d.revision FROM revision_effects e"
            " JOIN designs d ON d.design_id=e.design_id WHERE e.design_id=?",
            (design_id,)).fetchone()
        if row is None:
            raise MissingDependency("revision effect", design_id)
        result = dict(row)
        result["scope_job_ids"] = [r["job_id"] for r in self.db.execute(
            "SELECT job_id FROM effect_scopes WHERE design_id=? ORDER BY job_id", (design_id,))]
        result["dispositions"] = [dict(r) for r in self.db.execute(
            "SELECT * FROM dispositions WHERE design_id=? ORDER BY id", (design_id,))]
        return result

    def _job_at_effective(self, job_id):
        return self.db.execute(
            "SELECT j.*,d.product,d.revision FROM jobs j"
            " JOIN designs d ON d.design_id=j.effective_design_id WHERE j.job_id=?",
            (job_id,)).fetchone()

    def dispatch(self, kind, payload, occurred_at, event_id):
        if kind == "revision.effect":
            return self.effect(payload, occurred_at, event_id)
        if kind == "revision.disposition":
            return self.disposition(payload, occurred_at, event_id)
        if kind == "revision.withdraw":
            return self.withdraw(payload, occurred_at, event_id)
        raise DomainError("unknown_kind", f"unsupported revision event {kind}")
