/* deskd console — the office floor MODEL.
 *
 * One pure function, `Office.floorPlan(board, meetings, opts)`, joins the two
 * projections the console already serves — /api/board and /api/meetings — into
 * the answer the floor plan draws: who is in which room, and whose desk is
 * therefore empty. It is separate from office.html for two reasons:
 *
 *   1. The join is the only real logic on the page, and it is the part with
 *      edge cases (an agent in two meetings at once, an attendee with no desk,
 *      a room nobody has walked into yet). Rendering is then a dumb function of
 *      the model, and the model is testable without a browser — tests/
 *      test_web_console.py runs it under node when node is available.
 *   2. Occupancy is derived from the MEETINGS payload's attendees, never from
 *      board.agents[].meeting.active_meetings. The two disagree by design:
 *      the board's list is scoped to waiting/active/consensus/termination_
 *      pending, while /api/meetings also returns paused and escalated ones. A
 *      floor that seats someone from one list and empties their desk from the
 *      other can draw a person in two places at once; one source cannot.
 *
 * No fetching, no DOM, no formatting: a caller passes the clock in, so the SLA
 * countdowns are deterministic under test.
 */
(function (root) {
  "use strict";

  /** Seconds until `iso` (negative once it has passed); null if unparseable. */
  function secondsUntil(iso, now) {
    if (!iso) return null;
    var t = new Date(iso).getTime();
    if (isNaN(t)) return null;
    return (t - now) / 1000;
  }

  /** Up to two letters for an avatar. Display names win over role ids. */
  function initials(name) {
    var s = String(name == null ? "" : name).trim();
    if (!s) return "?";
    var parts = s.split(/[\s_\-.]+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return s.slice(0, 2).toUpperCase();
  }

  /** A DOM-id-safe anchor for a room, so a desk can link to the room it points at. */
  function roomAnchor(threadId) {
    return "room-" + String(threadId == null ? "" : threadId).replace(/[^A-Za-z0-9_-]/g, "-");
  }

  function isSeated(a) { return !!a.checked_in_at && !a.stopped_at; }

  function floorPlan(board, meetingList, opts) {
    opts = opts || {};
    var now = opts.now == null ? Date.now() : opts.now;
    // Never a literal "supervisor": the identity is host-configured and the
    // console reads it from /api/meeting-meta.
    var supervisorRole = opts.supervisorRole || "supervisor";
    var ladder = opts.ladder || [];
    var agents = (board && board.agents) || [];
    var byRole = {};
    agents.forEach(function (a) { byRole[a.role] = a; });

    // role -> the rooms they are sitting in RIGHT NOW. Usually zero or one; an
    // agent checked into two live meetings is drawn at both tables and its desk
    // says so, because that is the truth and hiding it would be worse.
    var seatedIn = {};
    var rooms = [];

    (meetingList || []).forEach(function (s) {
      var m = (s && s.meeting) || {};
      if (!m.thread_id) return;
      // The endpoint already excludes closed meetings; belt and braces, because
      // a closed room drawn with people in it is the one lie this page cannot
      // afford.
      if (m.state === "closed") return;

      var attendees = s.attendees || [];

      // Pending reply debts, soonest deadline first. `response_obligations`
      // carries resolved and waived rows too — only a pending one is somebody
      // still owing an answer.
      var owed = (s.response_obligations || [])
        .filter(function (o) { return o.status === "pending"; })
        .map(function (o) {
          return {
            role: o.owed_by, dueAt: o.due_at, sender: o.sender, kind: o.kind,
            messageId: o.message_id, secondsLeft: secondsUntil(o.due_at, now)
          };
        })
        .sort(function (a, b) {
          if (a.secondsLeft == null) return 1;
          if (b.secondsLeft == null) return -1;
          return a.secondsLeft - b.secondsLeft;
        });
      var owedByRole = {};
      owed.forEach(function (o) { if (!owedByRole[o.role]) owedByRole[o.role] = o; });

      // Wrapping up: who has NOT confirmed the pending end. Mirrors the
      // engine's _missing_confirmations — active REQUIRED attendees minus the
      // roles that voted confirm (the proposer's own confirm is recorded at
      // propose time, so it drops out here without a special case).
      var termination = null;
      if (s.termination) {
        var confirmed = {};
        (s.votes || []).forEach(function (v) {
          if (v.vote === "confirm") confirmed[v.role] = true;
        });
        termination = {
          proposer: s.termination.proposer,
          resolution: s.termination.resolution,
          createdAt: s.termination.created_at,
          votes: (s.votes || []).slice(),
          waitingOn: attendees.filter(function (a) {
            return a.required && isSeated(a) && !confirmed[a.role];
          }).map(function (a) { return a.role; })
        };
      }

      var seated = [], invited = [], left = [];
      attendees.forEach(function (a) {
        var agent = byRole[a.role] || null;
        var person = {
          role: a.role,
          name: (agent && agent.display_name) || a.role,
          isSupervisor: a.role === supervisorRole,
          // Nobody's desk: the supervisor is a human who visits, and a role can
          // also be retired out of the registry while its attendance row lives
          // on. Either way there is no desk to empty, only a chair to draw.
          hasDesk: !!agent,
          liveness: agent ? agent.liveness : null,
          required: !!a.required,
          invitedAt: a.invited_at,
          checkedInAt: a.checked_in_at,
          stoppedAt: a.stopped_at,
          owes: owedByRole[a.role] || null,
          voteOwed: !!(termination && termination.waitingOn.indexOf(a.role) >= 0)
        };
        if (a.stopped_at) { person.seat = "left"; left.push(person); }
        else if (a.checked_in_at) { person.seat = "seated"; seated.push(person); }
        else { person.seat = "invited"; invited.push(person); }
      });

      var room = {
        id: m.thread_id,
        anchor: roomAnchor(m.thread_id),
        agenda: m.agenda || "(no agenda)",
        state: m.state,
        mode: s.mode || "waiting",
        meetingType: m.meeting_type,
        priority: m.priority,
        calledBy: m.called_by,
        createdAt: m.created_at,
        updatedAt: m.updated_at,
        maxMessages: m.max_messages,
        messageCount: m.message_count,
        messagesRemaining: m.messages_remaining,
        waitTimeoutSeconds: m.wait_timeout_seconds,
        // A thread the idle deadline retired while its meeting row still says
        // `active`: the room is there, the door is locked. Say so rather than
        // drawing a working meeting nobody can speak in.
        threadRetired: !!(m.thread_status && m.thread_status !== "open"),
        threadStatus: m.thread_status,
        seated: seated, invited: invited, left: left,
        empty: seated.length === 0,
        humanPresent: seated.some(function (p) { return p.isSupervisor; }),
        nextOwed: owed.length ? owed[0] : null,
        owedCount: owed.length,
        termination: termination,
        wrappingUp: m.state === "termination_pending"
      };
      rooms.push(room);
      seated.forEach(function (p) {
        (seatedIn[p.role] = seatedIn[p.role] || []).push(
          { id: room.id, anchor: room.anchor, agenda: room.agenda });
      });
    });

    var desks = agents.map(function (a) {
      var meeting = a.meeting || {};
      var obligations = meeting.response_obligations || {};
      var inbox = a.inbox || {};
      var wake = a.wake || {};
      var ringing = null;
      if (wake.pending) {
        // Name the rung, not just its number: "L3" tells an operator nothing,
        // "human channel" tells them a person is being pulled in. The ladder is
        // host configuration, so it is read from /api/wake rather than guessed;
        // without it the level still shows, unnamed.
        var rung = (wake.max_level != null && ladder[wake.max_level]) || null;
        ringing = {
          pending: wake.pending,
          level: wake.max_level,
          channel: rung ? rung.channel : null,
          leavesMachine: !!(rung && rung.leaves_machine),
          wired: rung ? rung.wired !== false : true,
          terminal: !!(rung && rung.terminal)
        };
      }
      var away = seatedIn[a.role] || [];
      var liveness = a.liveness || "never";
      return {
        role: a.role,
        name: a.display_name || a.role,
        liveness: liveness,
        // The board's own predicate for "a turn is running right now" (engine:
        // LIVE_LIVENESS). Anything else — parked, crashed, ended, never started
        // — still HAS a desk; what it shows there is the last thing it said,
        // and the view must not present that as current work.
        live: liveness === "online" || liveness === "suspect",
        state: a.state,
        activity: a.activity,
        heartbeatAge: a.heartbeat_age_seconds,
        phase: a.phase,
        sessionDay: a.session_day,
        staleDay: !!a.stale_day,
        capabilities: a.capabilities || [],
        away: away,
        atDesk: away.length === 0,
        ringing: ringing,
        trays: {
          inbox: inbox.queued_count || 0,
          inboxUrgent: inbox.urgent_queued || 0,
          // The engine's own open-task set (a.tasks), not a re-derived one.
          tasks: (a.tasks || []).length,
          overdue: a.overdue_count || 0,
          hooks: (a.hooks || []).length,
          owed: obligations.pending || 0,
          owedDueAt: obligations.next_due_at || null,
          unread: meeting.unread_messages || 0
        }
      };
    });

    var stats = {
      agents: desks.length,
      atDesk: desks.filter(function (d) { return d.atDesk; }).length,
      // At a desk splits two ways, and the floor should say which: somebody
      // actually working, and somebody whose chair is warm but whose session is
      // parked or gone. Counting them together makes a dead desk look staffed.
      working: desks.filter(function (d) { return d.atDesk && d.live; }).length,
      quiet: desks.filter(function (d) { return d.atDesk && !d.live; }).length,
      inMeetings: desks.filter(function (d) { return !d.atDesk; }).length,
      ringing: desks.filter(function (d) { return !!d.ringing; }).length,
      rooms: rooms.length,
      roomsEmpty: rooms.filter(function (r) { return r.empty; }).length,
      roomsWrapping: rooms.filter(function (r) { return r.wrappingUp; }).length,
      // Debts across every live room, including ones owed by a role with no
      // desk — the count must match what the rooms show.
      owed: rooms.reduce(function (n, r) { return n + r.owedCount; }, 0),
      // Visitors at a table who do not work here: the supervisor, or a role
      // retired since it was invited.
      guests: rooms.reduce(function (n, r) {
        return n + r.seated.filter(function (p) { return !p.hasDesk; }).length;
      }, 0)
    };

    return { rooms: rooms, desks: desks, stats: stats, generatedAt: (board || {}).generated_at };
  }

  root.Office = {
    floorPlan: floorPlan,
    initials: initials,
    roomAnchor: roomAnchor,
    secondsUntil: secondsUntil
  };
  // `window` in the console, `globalThis` under node: the model is loaded by a
  // plain <script> tag AND unit-tested headlessly, with no build step for either.
})(typeof window !== "undefined" ? window : globalThis);
