import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, streamAdvance, streamReattempt, streamFillGaps } from "../api.js";
import { useToast } from "./ui/Toast.jsx";
import Dialog from "./ui/Dialog.jsx";
import { useStreamFlow } from "./ui/useStreamFlow.js";
import ProgressStream from "./ProgressStream.jsx";
import ResolutionOptions from "./ResolutionOptions.jsx";
import ConfirmDialog from "./ConfirmDialog.jsx";

// A standalone "resolve a roadblock" dialog, shared by the Operations resume
// flow and the Library "Resolve" action. Two ways to start:
//   - pass `initial` (a fallback_suggested / exhausted result) to seed the menu
//     directly — used to re-open a stalled operation.
//   - pass `autoReattempt` to kick off an in-place re-search on open and show
//     whatever menu comes back — used to resolve an already-added series.
// `series` needs {title, tvdbId} always, plus {instanceId, seriesId} when
// autoReattempt is set. Option payloads carry every id needed to dispatch.
export default function ResolveDialog({
  series,
  chainKey,
  initial = null,
  autoReattempt = false,
  onClose,
}) {
  const toast = useToast();
  const [roadblock, setRoadblock] = useState(initial);
  const [confirmRemove, setConfirmRemove] = useState(null);
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();
  const started = useRef(false);
  // Renamed from the stream flow's `busy` to avoid colliding with the
  // remove-confirm `busy` state above.
  const { flow, run, busy: streaming, canShowFooter } = useStreamFlow();

  function done(msg) {
    if (msg) toast(msg);
    qc.invalidateQueries({ queryKey: ["operations"] });
    qc.invalidateQueries({ queryKey: ["series"] });
    onClose();
  }

  function handleTerminal(res) {
    if (res.status === "fallback_suggested" || res.status === "exhausted") {
      setRoadblock(res);
      return;
    }
    if (res.status === "split") {
      done(
        `Split “${series.title}” — ${res.gapCount} episode${res.gapCount === 1 ? "" : "s"} ` +
          `now downloading; existing episodes kept.`
      );
      return;
    }
    if (res.status === "placed" || res.status === "added") {
      done(`Downloading “${series.title}” — release found ✓`);
      return;
    }
    done();
  }

  function runStream(fn, params) {
    setRoadblock(null);
    run(fn, { ...params, title: series.title }, {
      onResult: handleTerminal,
    });
  }

  // Library entry: re-search the existing series in place as soon as we open.
  useEffect(() => {
    if (autoReattempt && !started.current) {
      started.current = true;
      runStream(streamReattempt, {
        tvdbId: series.tvdbId,
        instanceId: series.instanceId,
        seriesId: series.seriesId,
        chainKey,
        nextIndex: 0,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function dispatchOption(o) {
    switch (o.action) {
      case "reattempt":
        runStream(streamReattempt, o.payload);
        break;
      case "walk_chain":
        runStream(streamAdvance, o.payload);
        break;
      case "fill_gaps":
        runStream(streamFillGaps, o.payload);
        break;
      case "leave":
        done(`Kept “${series.title}” monitored.`);
        break;
      case "remove":
        setConfirmRemove(o.payload);
        break;
      default:
        break;
    }
  }

  async function confirmRemoveSeries() {
    setBusy(true);
    try {
      await api.removeSeries(confirmRemove.instanceId, confirmRemove.seriesId);
      done(`Removed “${series.title}”.`);
    } catch (e) {
      toast(e.message, true);
      setBusy(false);
      setConfirmRemove(null);
    }
  }

  // Once a stream has ever run, stay on the progress/resolution view rather
  // than reverting to the "checking…" placeholder.
  const inFlight = flow.phase !== "idle";

  return (
    <>
      <Dialog
        title={series.title}
        label={`Resolve ${series.title}`}
        onClose={onClose}
        busy={streaming}
        subtitle={<span className="mono">tvdb {series.tvdbId}</span>}
        footer={
          canShowFooter ? (
            <button className="btn ghost" onClick={onClose}>Close</button>
          ) : null
        }
      >
        {inFlight && <ProgressStream steps={flow.steps} />}
        {roadblock && (
          <ResolutionOptions data={roadblock} series={series} onDispatch={dispatchOption} />
        )}
        {!inFlight && !roadblock && (
          <div style={{ padding: "8px 0", color: "var(--ink-dim)" }}>Checking availability…</div>
        )}
      </Dialog>
      {confirmRemove && (
        <ConfirmDialog
          title={`Remove “${series.title}”?`}
          message="Removes the empty series. Files already on disk are kept."
          confirmLabel="Remove"
          danger
          busy={busy}
          onConfirm={confirmRemoveSeries}
          onCancel={() => setConfirmRemove(null)}
        />
      )}
    </>
  );
}
