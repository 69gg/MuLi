/** Page/filter requests share one generation, including background SSE refreshes. */
export function createTeamPager(request, { limit = 50 } = {}) {
  let query = { limit, offset: 0, activeOnly: false };
  let generation = 0;
  return {
    get query() { return { ...query }; },
    invalidate() { generation += 1; },
    async load(changes = {}, read = request) {
      query = { ...query, ...changes };
      const current = ++generation;
      const params = new URLSearchParams({ limit: query.limit, offset: query.offset, active_only: query.activeOnly });
      try {
        const page = await read(`/team?${params}`);
        if (current !== generation) return null;
        query = { limit: page.limit, offset: page.offset, activeOnly: page.active_only };
        return page;
      } catch (error) {
        if (current !== generation) return null;
        throw error;
      }
    },
  };
}

/** Preserve local keyboard focus and disclosure state during live replacement. */
export function captureTeamView(root) {
  if (!root.querySelector('.team-filter')) return null;
  const focused = root.ownerDocument.activeElement;
  const inside = root.contains(focused);
  const action = inside && focused?.dataset?.action;
  return {
    inside,
    action: action || null,
    key: action ? Object.fromEntries(['id', 'session', 'active', 'direction'].filter(key => focused.dataset[key] !== undefined).map(key => [key, focused.dataset[key]])) : {},
    summary: inside && focused?.tagName === 'SUMMARY',
    disclosure: !!root.querySelector('.team-messages')?.open,
  };
}

export function restoreTeamView(root, saved) {
  if (!saved) return;
  const details = root.querySelector('.team-messages');
  if (details) details.open = saved.disclosure;
  if (!saved.inside) return;
  const matching = saved.action && [...root.querySelectorAll('[data-action]')].find(node =>
    node.dataset.action === saved.action && Object.entries(saved.key).every(([key, value]) => node.dataset[key] === value));
  const target = matching && !matching.disabled ? matching : saved.summary && details ? details.querySelector('summary') : root.querySelector('.view-heading h2');
  if (target) { if (target.tagName === 'H2') target.tabIndex = -1; target.focus({ preventScroll: true }); }
}
