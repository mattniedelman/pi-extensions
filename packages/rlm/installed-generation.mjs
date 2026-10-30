export function createInstalledGeneration(sidecar, descendants) {
  let previous = null;
  return function installedGeneration() {
    const observed = descendants();
    const candidates = observed.filter((child) => child.argv.length > 0 && child.argv.includes(sidecar));
    const candidatePids = new Set(candidates.map((child) => child.pid));
    const roots = candidates.filter((child) => !candidatePids.has(child.parent));
    if (roots.length !== 1) {
      previous = null;
      return null;
    }
    const members = new Set([roots[0].pid]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const child of observed) {
        if (members.has(child.parent) && !members.has(child.pid)) {
          members.add(child.pid);
          changed = true;
        }
      }
    }
    const generation = observed.filter((child) => members.has(child.pid));
    if (generation.length < 2 || generation.some((child) => child.argv.length === 0) ||
        generation.filter((child) => child.argv.includes(sidecar)).length !== 2) {
      previous = null;
      return null;
    }
    const signature = JSON.stringify(generation.map((child) => [child.pid, child.start, child.parent, child.argv]));
    if (signature !== previous) {
      previous = signature;
      return null;
    }
    return generation;
  };
}
