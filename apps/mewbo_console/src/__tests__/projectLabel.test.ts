import { describe, it, expect } from 'vitest';
import {
  AUTO_PROJECT,
  AUTO_PROJECT_LABEL,
  ProjectLabel,
} from '../utils/projectLabel';
import { ProjectSummary } from '../api/contracts';

const parent: ProjectSummary = { name: 'Acme', path: '/p', project_id: 'p1', source: 'managed' };
const worktree: ProjectSummary = {
  name: 'feat-slug',
  path: '/wt',
  project_id: 'wt1',
  source: 'managed',
  is_worktree: true,
  parent_project_id: 'p1',
  branch: 'feature/login',
};

describe('ProjectLabel', () => {
  const resolver = new ProjectLabel([parent, worktree]);

  it('passes through a plain (config) project name', () => {
    expect(resolver.resolve({ project: 'Assistant' })).toEqual({ label: 'Assistant', branch: null });
  });

  it('resolves a managed project UUID to its name', () => {
    expect(resolver.resolve({ project: 'managed:p1' })).toEqual({ label: 'Acme', branch: null });
  });

  it('resolves a worktree to its parent repo name plus branch', () => {
    expect(resolver.resolve({ project: 'managed:wt1' })).toEqual({
      label: 'Acme',
      branch: 'feature/login',
    });
  });

  it('falls back to repo (then a generic label) for an unknown managed id', () => {
    expect(resolver.resolve({ project: 'managed:gone', repo: 'cached-repo' }).label).toBe('cached-repo');
    expect(resolver.resolve({ project: 'managed:gone' }).label).toBe('Managed project');
  });

  it('uses repo when no project is set', () => {
    expect(resolver.resolve({ repo: 'bare-repo' })).toEqual({ label: 'bare-repo', branch: null });
  });
});

describe('ProjectLabel — the auto-select sentinel', () => {
  const resolver = new ProjectLabel([parent, worktree]);

  it('recognises the sentinel, including a value that round-tripped with whitespace', () => {
    expect(ProjectLabel.isAuto(AUTO_PROJECT)).toBe(true);
    expect(ProjectLabel.isAuto(' auto ')).toBe(true);
    expect(ProjectLabel.isAuto('Acme')).toBe(false);
    expect(ProjectLabel.isAuto(null)).toBe(false);
    expect(ProjectLabel.isAuto(undefined)).toBe(false);
  });

  it('names it rather than printing the bare wire token', () => {
    // "auto" on screen reads as a project somebody registered. The user must be
    // able to tell auto-select apart from a project that happens to be so named.
    expect(resolver.resolve({ project: AUTO_PROJECT })).toEqual({
      label: AUTO_PROJECT_LABEL,
      branch: null,
    });
    expect(resolver.resolve({ project: AUTO_PROJECT }).label).not.toBe(AUTO_PROJECT);
  });

  it('is NOT "Temporary directory" — the two states differ', () => {
    // Omitting `project` is a plain temp dir with no agent-driven selection;
    // the sentinel is a temp dir the agent is expected to move OUT of. A label
    // that conflated them would hide the whole mode from the user.
    expect(resolver.resolve({ project: AUTO_PROJECT }).label).not.toEqual(
      resolver.resolve({}).label,
    );
  });

  it('carries no repo slug — the sentinel names no directory, so no remote', () => {
    expect(resolver.repoSlug({ project: AUTO_PROJECT })).toBeNull();
  });

  it('resolves the concrete key once the agent has switched', () => {
    // The switch rewrites `context.project`, so nothing about auto mode lingers
    // in the label: a switched session reads exactly like a bound one.
    expect(resolver.resolve({ project: 'managed:p1' }).label).toBe('Acme');
  });
});
