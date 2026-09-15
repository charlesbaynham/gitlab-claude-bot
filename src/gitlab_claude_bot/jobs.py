import logging
import re
import time
from dataclasses import dataclass

from . import prompt, runner
from .config import Config
from .gitlab import GitLab, Project, User
from .runner import AgentResult
from .triggers import Trigger

log = logging.getLogger(__name__)

BRANCH_PREFIX = "bot/issue-"
SLUG_MAX = 40


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:SLUG_MAX].rstrip("-")


def branch_name(iid: int, title: str) -> str:
    return "-".join(part for part in (f"{BRANCH_PREFIX}{iid}", slug(title)) if part)


def find_existing_mr(gl: GitLab, project_id: int, bot_id: int, iid: int) -> dict | None:
    own = f"{BRANCH_PREFIX}{iid}"
    for mr in gl.project_open_mrs_by(project_id, bot_id):
        if mr["source_branch"] == own or mr["source_branch"].startswith(f"{own}-"):
            return mr
    return None


def job_name(trigger: Trigger) -> str:
    t = trigger.target
    return f"{t.project_id}-{t.kind[:2]}{t.iid}-{int(time.time())}"


@dataclass(frozen=True)
class _Plan:
    project: Project
    clone_from: Project
    target: dict
    clone_branch: str
    new_branch: str | None
    existing_mr: dict | None
    can_push: bool

    @property
    def work_branch(self) -> str:
        return self.new_branch or self.clone_branch


def _plan(gl: GitLab, bot: User, trigger: Trigger) -> _Plan:
    t = trigger.target
    project = gl.project(t.project_id)
    if t.kind == "merge_requests":
        mr = gl.merge_request(t.project_id, t.iid)
        from_fork = mr["source_project_id"] != project.id
        # a fork's branch exists only in the fork, so clone from there; the bot cannot push back to it
        clone_from = gl.project(mr["source_project_id"]) if from_fork else project
        return _Plan(project, clone_from, mr, mr["source_branch"], None, None, not from_fork)

    issue = gl.issue(t.project_id, t.iid)
    existing = find_existing_mr(gl, t.project_id, bot.id, t.iid)
    if existing:
        return _Plan(project, project, issue, existing["source_branch"], None, existing, True)
    return _Plan(project, project, issue, project.default_branch, branch_name(t.iid, issue["title"]), None, True)


def _reply(gl: GitLab, trigger: Trigger, body: str) -> None:
    t = trigger.target
    if trigger.discussion_id:
        gl.reply(t.project_id, t.kind, t.iid, trigger.discussion_id, body)
    else:
        gl.comment(t.project_id, t.kind, t.iid, body)


def _agent_failure(result: AgentResult, cfg: Config) -> str:
    if result.subtype == "timeout":
        return f"I couldn't complete this: the agent timed out after {cfg.job_timeout}s."
    return f"I couldn't complete this: the agent run failed ({result.subtype or 'unknown'})."


def _pushed_reply(gl: GitLab, trigger: Trigger, plan: _Plan, text: str, stat: str) -> str:
    t = trigger.target
    if t.kind == "merge_requests":
        return f"{text}\n\n{stat}"
    if plan.existing_mr:
        return f"Updated !{plan.existing_mr['iid']}.\n\n{text}"
    mr = gl.create_mr(
        plan.project.id,
        plan.work_branch,
        plan.project.default_branch,
        title=f"Resolve #{t.iid}: {plan.target['title']}",
        description=f"{text}\n\nCloses #{t.iid}".strip(),
        assignee_id=trigger.author_id,
    )
    return f"Opened !{mr['iid']}: {text.splitlines()[0] if text else 'see the merge request.'}"


def _run(gl: GitLab, cfg: Config, bot: User, trigger: Trigger, name: str) -> bool:
    t = trigger.target
    plan = _plan(gl, bot, trigger)
    ctx = prompt.Context(
        project_path=plan.project.path_with_namespace,
        kind=t.kind,
        iid=t.iid,
        title=plan.target["title"],
        description=plan.target.get("description") or "",
        branch=plan.work_branch,
        discussions=gl.discussions(t.project_id, t.kind, t.iid),
        trigger_note_ids=trigger.note_ids,
        trigger_body=None,
    )

    with runner.JobDir(cfg.work_dir, name) as jobdir:
        repo = runner.clone(cfg, plan.clone_from, plan.clone_branch, jobdir, bot, plan.clone_from.default_branch)
        if plan.new_branch:
            runner.create_branch(repo, plan.new_branch)
        result = runner.run_agent(
            cfg,
            jobdir,
            prompt.system_prompt(bot.username),
            prompt.build(ctx, bot.username, trigger.action),
            bot,
            name,
        )
        if result.is_error:
            log.error("job %s: agent failed (%s), stderr tail:\n%s", name, result.subtype, result.stderr_tail)
            _reply(gl, trigger, _agent_failure(result, cfg))
            return False

        text = result.result.strip()
        changes = runner.harvest(repo)
        leftovers = f"Resolve #{t.iid}: {plan.target['title']}" if t.kind == "issues" else "Apply review feedback (bot)"
        committed = runner.commit_leftovers(repo, leftovers)
        if not (changes.commits or committed):
            _reply(gl, trigger, text or "I made no changes.")
            return True

        stat = f"```\n{runner.diff_stat(repo).strip()}\n```"
        if not plan.can_push:
            fork = "I couldn't push these changes: this merge request comes from a fork, which the bot can't push to."
            _reply(gl, trigger, f"{text}\n\n{fork}\n\n{stat}")
            return True
        runner.push(cfg, repo, plan.work_branch)

    _reply(gl, trigger, _pushed_reply(gl, trigger, plan, text, stat))
    return True


def run_job(gl: GitLab, cfg: Config, bot: User, trigger: Trigger, name: str | None = None) -> bool:
    name = name or job_name(trigger)
    log.info("job %s: %s on %s by @%s", name, trigger.action, trigger.target.key, trigger.author)
    try:
        return _run(gl, cfg, bot, trigger, name)
    except Exception as error:
        log.exception("job %s failed", name)
        try:
            _reply(gl, trigger, f"I couldn't complete this: {type(error).__name__}. Check the bot's log for job {name}.")
        except Exception:
            log.exception("job %s: could not post the failure comment either", name)
        return False
