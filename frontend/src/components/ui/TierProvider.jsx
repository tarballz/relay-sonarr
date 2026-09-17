import { createContext, useContext, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api.js";
import { buildTiers, tierClass, tierLabel } from "../../lib/tiers.js";

const TierContext = createContext({});

export function useTiers() {
  const registry = useContext(TierContext);
  return useMemo(
    () => ({
      registry,
      cls: (id) => tierClass(id, registry),
      label: (id, fallbackName) => tierLabel(id, registry, fallbackName),
    }),
    [registry],
  );
}

export function TierProvider({ children }) {
  // Reuses the settings query the Settings page and SearchAdd already run.
  const { data } = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const registry = useMemo(() => buildTiers(data?.instances), [data]);
  return <TierContext.Provider value={registry}>{children}</TierContext.Provider>;
}
