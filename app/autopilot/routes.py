"""Explicit dry-run/opt-in endpoints. No endpoint accepts reviewer-provided approval."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from .runner import redact

router = APIRouter()


@router.post("/api/autopilot/import")
async def import_markdown(request: Request):
    from .importer import MAX_TOTAL, documents, prepare

    try:
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_TOTAL + 128 * 1024:
                raise ValueError("total upload size limit exceeded")
            chunks.append(chunk)
        request._body = b"".join(chunks)  # Bound the body before the multipart parser allocates files.
        async with request.form(max_files=200, max_fields=10) as form:
            uploads = []
            for key, item in form.multi_items():
                if key != "files" or not hasattr(item, "filename"):
                    raise ValueError("upload only files under the files field")
                uploads.append((item.filename, await item.read(MAX_TOTAL + 1)))
        return prepare(documents(uploads))
    except (ValueError, TypeError, UnicodeError, KeyError) as e:
        raise error(e)


@router.post("/api/autopilot/import/convert")
async def convert_markdown(request: Request, body: dict):
    from .importer import convert

    try:
        raw = convert(body)
        return dict(manifest=raw, preview=await controller(request).dry_run(raw))
    except (ValueError, TypeError, KeyError) as e:
        raise error(e)


@router.post("/api/autopilot/workflows/{identity}/nodes/{name}/decision")
async def owner_decision(request: Request, identity: str, name: str, body: dict):
    try:
        if body.get("owner_action") is not True:
            raise ValueError("explicit owner action is required")
        return controller(request).owner_decision(
            identity + ":" + name, body["expected_target"], body["action"], body.get("note", "")
        )
    except (ValueError, TypeError, KeyError) as e:
        raise error(e)


@router.post("/api/autopilot/workflows/{identity}/revisions/{revision_id}/decision")
async def revision_decision(request: Request, identity: str, revision_id: str, body: dict):
    from . import planning

    ap = controller(request)
    try:
        r = ap.ledger.get("plan_revisions", revision_id)
        if (
            not r
            or r["workflow_id"] != identity
            or body.get("owner_action") is not True
            or body.get("action") not in ("approve", "reject")
        ):
            raise ValueError("explicit owner decision for this workflow revision is required")
        planning.apply(
            ap,
            r,
            expected=body["expected_target"],
            approve=body["action"] == "approve",
            reject=body["action"] == "reject",
        )
        return ap.ledger.view(identity)
    except (ValueError, TypeError, KeyError) as e:
        raise error(e)


def controller(request):
    return request.app.state.manager.autopilot


def error(e):
    return HTTPException(400, detail=dict(message=redact(str(e))))


@router.get("/autopilot", response_class=HTMLResponse)
async def page(request: Request):
    from app.routes import TEMPLATES

    return TEMPLATES.TemplateResponse(request=request, name="autopilot.html", context={})


@router.post("/api/autopilot/dry-run")
async def dry_run(request: Request, body: dict):
    try:
        return await controller(request).dry_run(body.get("manifest"))
    except (ValueError, TypeError, KeyError) as e:
        raise error(e)
    except Exception as e:
        import yaml

        if isinstance(e, yaml.YAMLError):
            raise error(ValueError("invalid safe YAML manifest"))
        raise


@router.post("/api/autopilot/workflows")
async def register(request: Request, body: dict):
    try:
        if body.get("opt_in") is not True:
            raise ValueError("explicit workflow opt-in is required")
        result = await controller(request).register(body.get("manifest"), body.get("expected_digest"))
        if body.get("start") is True:
            return await controller(request).control(result["id"], "start")
        return result
    except (ValueError, TypeError, KeyError) as e:
        raise error(e)


@router.get("/api/autopilot/workflows")
async def workflows(request: Request):
    ap = controller(request)
    return dict(
        workflows=[
            dict(id=w["id"], name=w["plan"]["name"], state=w["state"], reason=w["reason"], generation=w["generation"])
            for w in ap.ledger.rows("workflows")
        ]
    )


@router.get("/api/autopilot/workflows/{identity}")
async def workflow(request: Request, identity: str):
    try:
        return controller(request).ledger.view(identity)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


@router.post("/api/autopilot/workflows/{identity}/{action}")
async def control(request: Request, identity: str, action: str):
    try:
        return await controller(request).control(identity, action)
    except ValueError as e:
        raise error(e)


@router.get("/api/autopilot/backlog")
async def backlog(request: Request):
    ap = controller(request)
    rows = []
    from app.completion import parse_result
    import json

    for task in ap.manager.db.list_tasks():
        if task["status"] != "completed" or task["task_outcome"] != "needs_review":
            continue
        rules = json.loads(task["completion_contract"])
        result = parse_result(task["semantic_result"])
        if ap.ledger.node_for_task(task["id"]):
            category = "already_registered"
        elif rules.get("manual_approval"):
            category = "manual_required"
        elif not result:
            category = "structured_result_missing"
        elif task["evidence_result"] != "PASS" or not rules.get("validation_commands"):
            category = "validation_required"
        elif task["outcome_reason"] == "Completion checks passed; automatic completion approval is disabled.":
            category = "policy_auto_disabled"
        else:
            category = "unreviewed"
        import shlex

        commands = [shlex.split(cmd) if isinstance(cmd, str) else cmd for cmd in rules.get("validation_commands", [])]
        try:
            from .git import changed_paths

            paths = sorted(await changed_paths(task["worktree"], task["base_sha"]))
        except Exception:
            paths = []
        rows.append(
            dict(
                task_id=task["id"],
                name=task["name"],
                category=category,
                repository=task["repository"],
                contract=rules,
                paths=paths,
                validation=dict(commands=commands, artifacts=rules.get("required_paths", [])),
                automatic_policy="opt-in and registered validation profile required",
            )
        )
    return dict(tasks=rows)


@router.post("/api/autopilot/backlog/dry-run")
async def backlog_dry_run(request: Request, body: dict):
    """No partial task enrollment: validate all selected contracts before a workflow is registered."""
    ap = controller(request)
    selected = body.get("tasks", [])
    if not isinstance(selected, list) or not selected:
        raise error(ValueError("select backlog tasks with edit paths and mandatory validation profiles"))
    import json

    try:
        nodes = []
        repository = None
        for selection in selected:
            task = ap.manager.db.get_task(selection["task_id"])
            if not task or ap.ledger.node_for_task(task["id"]):
                raise ValueError("task missing or already registered")
            if repository and repository != task["repository"]:
                raise ValueError("one backlog workflow must use one repository")
            repository = task["repository"]
            nodes.append(
                dict(
                    id=task["id"],
                    task_id=task["id"],
                    kind=selection.get("kind", "code"),
                    prompt=task["prompt"],
                    paths=selection["paths"],
                    validation=selection["validation"],
                    contract=json.loads(task["completion_contract"]),
                )
            )
        raw = dict(
            version=1,
            name=body.get("name", "Needs Review import"),
            repository=repository,
            base_ref=body.get("base_ref", "main"),
            nodes=nodes,
            permissions=body.get("permissions", {}),
            policy=body.get("policy", {}),
        )
        preview = await ap.dry_run(raw)
        return dict(manifest=raw, preview=preview)
    except (ValueError, TypeError, KeyError) as e:
        raise error(e)


@router.get("/api/autopilot/tasks/{task_id}")
async def task_review(request: Request, task_id: str):
    ap = controller(request)
    n = ap.ledger.node_for_task(task_id)
    if not n:
        return dict(autopilot=False)
    w = ap.ledger.view(n["workflow_id"])
    return dict(
        autopilot=True,
        workflow_id=w["id"],
        workflow_state=w["state"],
        node=next(x for x in w["nodes"] if x["id"] == n["id"]),
        approval_kind="AI review accepted; separate from manual override and GitHub PR approval"
        if n["state"] == "accepted"
        else "Autopilot review pending; no AI acceptance, manual override or GitHub PR approval",
    )
