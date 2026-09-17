// Which instance is which, in one place: CSS class plus the short badge label.
// The registry is built from the instance list the backend reports; the id sniff
// below is the fallback for an instance the registry hasn't loaded yet.

const KNOWN = [
  { test: (id) => id.includes("4k"), cls: "t4k", label: "4K" },
  { test: (id) => id.includes("1080"), cls: "t1080p", label: "1080P" },
];

function sniff(instanceId) {
  const id = (instanceId || "").toLowerCase();
  return KNOWN.find((k) => k.test(id)) || { cls: "tdefault", label: null };
}

export function buildTiers(instances = []) {
  const registry = {};
  for (const inst of instances || []) {
    if (!inst?.id) continue;
    const { cls, label } = sniff(inst.id);
    registry[inst.id] = { id: inst.id, name: inst.name || inst.id, label: label || inst.name || inst.id, cls };
  }
  return registry;
}

export function tierClass(instanceId, registry) {
  return registry?.[instanceId]?.cls || sniff(instanceId).cls;
}

export function tierLabel(instanceId, registry, fallbackName) {
  const known = registry?.[instanceId];
  if (known) return known.label;
  return sniff(instanceId).label || fallbackName || instanceId || "";
}
