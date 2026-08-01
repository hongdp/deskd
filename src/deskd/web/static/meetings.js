/* deskd meetings — pure operational-state model.
 *
 * A meeting has two truthful states: its protocol phase (`state`) and whether
 * its mailbox accepts messages (`thread_status`). Idle expiry changes only the
 * mailbox, so every UI decision must go through this model instead of guessing
 * that an `active` protocol always has an open thread.
 */
(function (root) {
  "use strict";

  var STOPPED = ["paused", "escalated", "closed"];
  var DISCUSSION = ["waiting", "active", "consensus"];

  function effectiveState(m) {
    m = m || {};
    if (m.effective_state) return m.effective_state;
    var lifecycle = m.state || "unknown";
    var thread = m.thread_status || "open";
    if (STOPPED.indexOf(lifecycle) >= 0) return lifecycle;
    if (STOPPED.indexOf(thread) >= 0) return thread;
    return lifecycle;
  }

  function stateMismatch(m) {
    m = m || {};
    if (typeof m.state_in_sync === "boolean") return !m.state_in_sync;
    var lifecycle = m.state || "unknown";
    var expected = STOPPED.indexOf(lifecycle) >= 0 ? lifecycle : "open";
    return (m.thread_status || "open") !== expected;
  }

  function autoResumesOnSend(m) {
    m = m || {};
    if (m.thread_status !== "paused" || m.thread_stop_reason !== "idle timeout") {
      return false;
    }
    return DISCUSSION.indexOf(m.state) >= 0;
  }

  function resumesOnJoin(m) {
    m = m || {};
    return m.state === "escalated"
      && effectiveState(m) === "escalated";
  }

  function canSend(m, hasPendingTermination) {
    m = m || {};
    // An escalation can wrap a still-pending termination proposal. Joining
    // restores that protocol state, where the generic composer has no explicit
    // reply target and a normal decision would be refused. Make the user resume
    // first so the proposal/vote panel becomes visible again.
    if (resumesOnJoin(m)) return !hasPendingTermination;
    var legalPhase = DISCUSSION.indexOf(m.state) >= 0;
    if (!legalPhase) return false;
    return (m.thread_status || "open") === "open"
      || autoResumesOnSend(m);
  }

  function canResume(m) {
    return STOPPED.indexOf(effectiveState(m)) >= 0;
  }

  function obligationsPayable(m) {
    m = m || {};
    return (m.thread_status || "open") === "open"
      && (m.state === "active" || m.state === "consensus");
  }

  root.MeetingView = {
    effectiveState: effectiveState,
    stateMismatch: stateMismatch,
    autoResumesOnSend: autoResumesOnSend,
    resumesOnJoin: resumesOnJoin,
    canSend: canSend,
    canResume: canResume,
    obligationsPayable: obligationsPayable
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
