# Sandboxed Execution

## Confining what a shell reaches

How the Linux kernel confines a shell subprocess to the files its session is entitled to read.

Every other built-in tool takes a path argument, and Mewbo validates that argument before anything
runs. The shell takes an opaque command string. There is nothing in it to validate, so the
confinement has to happen one layer down, at the moment the subprocess is created.

For the tools themselves, see [Built-in Tools](features-builtin-tools.md). For the config keys
named here, see [Configuration](configuration.md#agent).

---

## Why a command cannot be checked

Setting a working directory tells you where a command starts. It tells you nothing about what the
command may read. `cat`, `grep`, `awk` and a one line Python script each walk straight out of a
confined starting directory, and they do it with ordinary arguments that no validator can
distinguish from legitimate ones.

That is not a theory about what could happen. It was measured from a session whose working
directory was confined to one project. `grep -r`, `cat` and `python3 open()` each returned a
sibling project's secret verbatim, exit code 0.

Inspecting the command text is not a fix either. A model can write the same read a hundred ways,
and a check that has to understand shell quoting, pipelines, `eval` and every interpreter on the
box is a check that will be wrong. So Mewbo does not try. The control binds below the tool layer,
where the kernel refuses the syscall regardless of which binary ran.

---

## What Landlock does

Landlock is a Linux security module that lets an ordinary unprivileged process restrict itself. The
restriction survives `execve`, so a process can confine itself and then become the real command.
Two properties make it the right shape here.

- **It only ever removes access.** Landlock cannot grant anything the process could not already
  reach. Your normal filesystem permissions still apply underneath, so a broad grant costs nothing
  and a confined shell can never do more than an unconfined one.
- **Rulesets intersect rather than replace.** A child that applies a second, wider ruleset still
  cannot reach the addition. A sub-agent is therefore structurally unable to widen the scope it
  inherited.

Mewbo builds the ruleset in the parent process and applies it in the child between fork and exec.
The implementation is [`landlock.py`](repo:packages/mewbo_tools/src/mewbo_tools/integration/landlock.py),
and the shell's spawn seam is
[`shell_session.py`](repo:packages/mewbo_tools/src/mewbo_tools/integration/shell_session.py).

### It is a deny list, and that is deliberate

The obvious design is to enumerate what a shell may reach. Mewbo built that first and it failed
twice, both times silently. Enumerating what is allowed means enumerating the entire runtime, and
anything missed breaks the harness instead of confining it. A missing `site-packages` surfaces as
`ModuleNotFoundError`, not as a denial, because an unreadable directory just makes Python's path
finder come up empty.

Naming what must be denied is a short list a person can hold in their head. Naming what must be
allowed is every dependency of every command an agent might run.

Landlock itself has no deny rule. It grants beneath a path and refuses everything unnamed. So a
denial is compiled into grants. Mewbo walks the ancestry of each denied path and, at every level,
grants the siblings that do not lead to a denial. That is a directory listing at build time rather
than a list anyone maintains. The source records about 95 rules taking roughly a millisecond to
compile and apply on a real deployment shape, and 28 rules in a container. Those numbers come from
the implementation notes and were not re-measured for this page.

---

## What is denied by default

`agent.shell_sandbox` is on by default. When it is on, a shell subprocess is denied three things.

- **Every configured project other than the session's active one.** A session bound to no project
  reaches no configured project at all. Binding one reveals exactly that project.
- **Mewbo's own source trees and the directory holding `app.json`.** That directory holds the API
  keys, so the harness hides itself unconditionally under `agent.harness_self_deny`, which is on by
  default. The paths are derived from where Mewbo is installed rather than configured, because a
  container runs from a source tree and a pip install runs from `site-packages`.
- **Anything listed in `agent.shell_denied_paths`.**

Everything else stays reachable. The interpreter, system libraries, the tools on `PATH` and the
home directory all work with no enumeration, which is exactly what the deny list buys you.

One denial is refused rather than honoured. Mewbo will not deny a directory that contains the
running Python runtime, and it will not deny the filesystem root. Denying those does not confine an
agent, it stops every command it could run. A refusal is logged as a warning so the misconfiguration
is visible rather than swallowed.

---

## Widening and narrowing the scope

Four keys under `agent` control this, and their real defaults are recorded in
[`app.schema.json`](repo:configs/app.schema.json).

| Key | Default | What it does |
|---|---|---|
| `agent.shell_sandbox` | `true` | Applies the Landlock confinement to shell subprocesses |
| `agent.harness_self_deny` | `true` | Hides Mewbo's own source and the directory holding `app.json` |
| `agent.shell_denied_paths` | unset | Extra absolute directories denied on top of the defaults |
| `agent.server_sandbox` | `false` | Extends the same confinement to spawned MCP and language servers |

To widen the scope for one project, name the extra directory under `allowed_paths` on that project.
A re-admitted path wins over a denial, which is what lets a session reach a sibling checkout that is
itself a configured project.

```json title="configs/app.json"
{
  "projects": {
    "api": {
      "path": "/srv/projects/api",
      "allowed_paths": ["/srv/projects/shared-protos"]
    }
  }
}
```

Turn `agent.harness_self_deny` off when Mewbo's own packages are the work rather than internals to
hide. The session then reaches them like any other project. Leave it on everywhere else. An
unwanted denial is immediate and local on a workstation, where one line fixes it, while a missing
denial on a deployment is invisible until someone reads the keys.

`agent.path_scope_to_active_project` is the argument validated half of the same boundary, covering
file read, file edit, directory listing and LSP. It is a separate switch because `shell_sandbox`
also decides whether a kernel mechanism applies at all, so a deployment on an older kernel that
turns the sandbox off should not silently lose the argument check too.

---

## Where the confinement is applied

The shell tools are the main case, and several other spawn seams take the same scope object. There
is one deny list, not several.

- The shell tool and background shell sessions.
- The `` !`command` `` preprocessor in skills.
- The verification pipeline runner.
- The wiki's git executor.
- MCP stdio servers and language servers, but only when `agent.server_sandbox` is also on.

The last of those cannot take a spawn hook, because the process is created inside somebody else's
library. Mewbo prefixes the server's command line with a small launcher that applies the ruleset to
itself and is then replaced by the real server, which inherits the confinement.
[`sandbox_launcher.py`](repo:packages/mewbo_tools/src/mewbo_tools/integration/sandbox_launcher.py)
is that shim. It is off by default because a denied path inside a server surfaces as a missing file
rather than as a refusal, which is the same silent failure shape that sank the original allowlist.

One spawn is deliberately left unconfined. The scip-python resolver used during wiki indexing runs
unscoped, because a scope limited to the project root hides the interpreter's `site-packages`, which
fails the resolver's own dependency probe and drops every cross file edge. It is a first party read
only indexer whose arguments Mewbo builds, not a model authored command.

---

## When part of the scope cannot be applied

A ruleset is compiled fresh for every spawn, and a compile can hit a transient error. A directory
might be removed by another process between the moment it was listed and the moment it was opened.
A mount can go away. An fd limit can be reached.

Mewbo distinguishes three outcomes rather than collapsing them into one.

- **A single grant could not be added.** That path is dropped and the ruleset is kept. The shell
  runs and the scope is narrower than intended. A warning names the paths that stayed unreachable,
  because a path the session needed will surface inside the shell as a permission error rather than
  as a refusal to start.
- **The kernel has no Landlock.** Nothing is enforced and the spawn proceeds exactly as it would
  have without the feature.
- **Landlock is present and the ruleset was still refused.** The spawn raises rather than
  continuing. A model authored shell command has no claim to run, and an operator believing it
  confined when it is not is the whole failure.

That distinction is the fix for a real defect. A single failed rule used to discard the ruleset and
return the same value that means "no Landlock here", so one lost grant produced a completely
unconfined shell. Narrowing is safe by construction, because an ungranted path is simply
unreachable. Removing the sandbox is not.

A child that cannot apply its ruleset writes a line to standard error and exits rather than running
unconfined. Without that line, a refused spawn would be exit code 127 and an empty buffer, which is
indistinguishable from "command not found".

---

## On a kernel without Landlock

Nothing is confined, one line is logged, and the run proceeds. A missing sandbox never fails a run.

Landlock has grown capabilities across kernel versions. Mewbo probes the running kernel once per
process and masks its ruleset down to what that kernel accepts, so an older supported kernel gets a
working sandbox rather than an error. Anything that is not Linux, including
macOS, takes the same degraded path. The feature is gracefully absent there rather than an import
failure.

If you rely on this boundary, check the log at startup. A sandbox that silently did nothing looks
exactly like one that worked.

---

## What it does not protect against

Volunteering the limits is more useful than being asked about them later.

- **Network access.** The ruleset Mewbo builds handles filesystem access classes only and sets the
  network field to zero. A confined command can still open sockets and reach anything the host can
  reach. Landlock is not an egress control here.
- **Unix sockets.** Landlock does not gate them, so a reachable Docker socket remains reachable and
  is an escape from any filesystem confinement. The cure is topology, which means not mounting the
  socket. This limit is recorded in the package's own engineering notes and was not re-measured for
  this page.
- **Anything legitimately granted.** The session's own project is fully reachable, including its
  `.git` directory and any credentials checked into it. The sandbox scopes which data a session may
  touch. It is not a review of what that data contains.
- **A second path to the same files.** The denial is path prefix based, so a bind mount that exposes
  a denied directory under a second name leaves it reachable by that second name. Resolving symlinks
  does not collapse a bind mount. Mewbo probes for this at startup and logs both names of any
  directory that is the same file, because it cannot be denied away without also denying legitimate
  project mounts.
- **A directory created mid command.** The compiled grants are a snapshot taken at spawn time, so a
  new entry created directly inside an expanded ancestor is invisible until the next spawn. The
  window is one command wide, and the default deny set does not reach this case.
- **Privilege, as opposed to data.** The sandbox is keyed on the session's bound project, not on the
  agent's containment tier. Those are different axes. A root agent has full privilege and is still
  scoped to its project's data.
