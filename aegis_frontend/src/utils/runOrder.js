export function newestRunsFirst(runs) {
  const submittedAt = (run) => {
    const value = Date.parse(run.created_at || run.started_at || run.finished_at || '')
    return Number.isFinite(value) ? value : 0
  }
  return [...runs].sort((left, right) => submittedAt(right) - submittedAt(left))
}
