"""
Task operations — the write layer for Tasks.

Every mutation records a TaskEvent so the append-only audit trail stays complete
even though the Task itself is editable. `create_task` is idempotent when given a
`dedupe_key`: the rule engine and the fermentation-nutrition path (slice C) both
lean on that to avoid re-creating the same task on every run.
"""
from django.db import transaction
from django.utils import timezone

from cellar.models import Task, TaskEvent


def _op(actor):
    return actor if (actor and getattr(actor, "is_authenticated", False)) else None


@transaction.atomic
def create_task(*, title, body="", due_date=None, assignee=None, lot=None,
                container=None, rack=None, addition=None, rule=None,
                dedupe_key=None, payload=None, actor=None):
    """Create a task, or return the existing one if `dedupe_key` already exists.
    Returns (task, created)."""
    op = _op(actor)
    if dedupe_key:
        task, created = Task.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults=dict(title=title, body=body, due_date=due_date, assignee=assignee,
                          lot=lot, container=container, rack=rack, addition=addition,
                          rule=rule, payload=payload or {}, created_by=op))
        if not created:
            return task, False
    else:
        task = Task.objects.create(
            title=title, body=body, due_date=due_date, assignee=assignee,
            lot=lot, container=container, rack=rack, addition=addition,
            rule=rule, payload=payload or {}, created_by=op)
        created = True

    TaskEvent.objects.create(
        task=task, kind=TaskEvent.Kind.CREATED, operator=op,
        detail=(f"assigned to {assignee}" if assignee else "unassigned"))
    return task, created


@transaction.atomic
def complete_task(task, actor=None, detail=""):
    if task.status == Task.Status.COMPLETED:
        return task
    task.status = Task.Status.COMPLETED
    task.completed_at = timezone.now()
    task.completed_by = _op(actor)
    task.save(update_fields=["status", "completed_at", "completed_by"])
    TaskEvent.objects.create(task=task, kind=TaskEvent.Kind.COMPLETED,
                             detail=detail, operator=_op(actor))
    return task


@transaction.atomic
def reopen_task(task, actor=None):
    task.status = Task.Status.OPEN
    task.completed_at = None
    task.completed_by = None
    task.save(update_fields=["status", "completed_at", "completed_by"])
    TaskEvent.objects.create(task=task, kind=TaskEvent.Kind.REOPENED, operator=_op(actor))
    return task


@transaction.atomic
def reassign_task(task, assignee, actor=None):
    old = task.assignee
    task.assignee = assignee
    task.save(update_fields=["assignee"])
    TaskEvent.objects.create(
        task=task, kind=TaskEvent.Kind.REASSIGNED, operator=_op(actor),
        detail=f"{old or 'unassigned'} → {assignee or 'unassigned'}")
    return task


@transaction.atomic
def delete_task(task, actor=None):
    """Soft delete — hidden from the UI, kept on the ledger."""
    task.status = Task.Status.DELETED
    task.save(update_fields=["status"])
    TaskEvent.objects.create(task=task, kind=TaskEvent.Kind.DELETED, operator=_op(actor))
    return task


@transaction.atomic
def edit_task(task, *, title=None, body=None, due_date=..., assignee=..., actor=None):
    changed = []
    if title is not None and title != task.title:
        task.title = title; changed.append("title")
    if body is not None and body != task.body:
        task.body = body; changed.append("body")
    if due_date is not ... and due_date != task.due_date:
        task.due_date = due_date; changed.append("due date")
    if assignee is not ... and assignee != task.assignee:
        return reassign_task(task, assignee, actor=actor)
    if changed:
        task.save(update_fields=changed if "due date" not in changed
                  else [c.replace("due date", "due_date") for c in changed])
        TaskEvent.objects.create(task=task, kind=TaskEvent.Kind.EDITED,
                                 detail=", ".join(changed), operator=_op(actor))
    return task


# ---------------------------------------------------------------- read helpers
def open_tasks(assignee=None, lot=None):
    qs = Task.objects.filter(status=Task.Status.OPEN).select_related(
        "assignee", "lot", "container", "rack", "rule")
    if assignee is not None:
        qs = qs.filter(assignee=assignee)
    if lot is not None:
        qs = qs.filter(lot=lot)
    return qs.order_by("due_date", "created_at")
