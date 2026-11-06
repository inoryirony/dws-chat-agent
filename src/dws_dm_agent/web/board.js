/* Shared operations board: 等我裁决 / 在跑会话 / 任务 / 抽屉 / 铃 / 新会话。
 *
 * 两套设计（Metropolis 与 Diamond）共用本文件，差异只在各自 CSS 的 token
 * 与版式；功能、文案、交互必须保持一致。页面只需给出三个锚点：
 *   [data-dws-board]        面板容器
 *   [data-dws-bell]         通知按钮（可选）
 *   [data-dws-new-session]  新会话按钮（可选）
 */
(function () {
  "use strict";

  const ICONS = {
    bell: "M12 3a5 5 0 0 0-5 5v3l-1.6 2.4A1 1 0 0 0 6.2 15h11.6a1 1 0 0 0 .8-1.6L17 11V8a5 5 0 0 0-5-5ZM9.5 18a2.5 2.5 0 0 0 5 0",
    plus: "M12 5v14M5 12h14",
    check: "m5 13 4 4L19 7",
    send: "M5 12h14M13 6l6 6-6 6",
    chat: "M20 15a3 3 0 0 1-3 3H8l-4 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3Z",
    close: "M6 6l12 12M18 6 6 18",
    chevron: "m6 9 6 6 6-6"
  };

  const STATUS = {
    sent: ["已回复", "success"], shadow: ["影子回复", "info"], no_reply: ["无需回复", "muted"],
    silent: ["已处理未回复", "muted"], human_cooldown: ["人工接管", "warning"],
    refused: ["安全拒绝", "danger"], error: ["处理失败", "danger"],
    stale_supplement: ["补充后重算", "info"], invalid_reply: ["回复重算", "info"]
  };

  const PHASES = [
    ["stabilizing", "合并消息"], ["screening", "规则检查"], ["routing", "前置节点"],
    ["acknowledged", "已确认"], ["worker", "执行节点"], ["steering", "补充已合并"],
    ["verifying", "代码核验"], ["freshness", "发送前复查"], ["sending", "发送"]
  ];

  function icon(name, cls) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("class", cls || "dws-icon");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", ICONS[name] || ICONS.chat);
    svg.append(path);
    return svg;
  }

  const node = (tag, cls, text) => {
    const value = document.createElement(tag);
    if (cls) value.className = cls;
    if (text !== undefined && text !== null) value.textContent = String(text);
    return value;
  };
  const clear = value => { while (value.firstChild) value.removeChild(value.firstChild); };
  const list = value => Array.isArray(value) ? value : [];
  const clock = value => {
    if (!value) return "-";
    const at = new Date(value);
    return Number.isNaN(at.getTime()) ? "-" : new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit", minute: "2-digit", second: "2-digit"
    }).format(at);
  };
  const elapsed = start => {
    if (!start) return "";
    const seconds = Math.max(0, Math.floor((Date.now() - new Date(start).getTime()) / 1000));
    return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  };
  const within24h = value => {
    const at = new Date(value).getTime();
    return Number.isFinite(at) && Date.now() - at < 86400000;
  };

  function reconcile(container, items, keyOf, make, update) {
    const keep = new Set(items.map(keyOf));
    [...container.children].forEach(child => { if (!keep.has(child.dataset.key)) child.remove(); });
    items.forEach((item, index) => {
      const key = keyOf(item);
      let row = [...container.children].find(child => child.dataset.key === key);
      if (!row) {
        row = make(item);
        row.dataset.key = key;
      }
      if (container.children[index] !== row) container.insertBefore(row, container.children[index] || null);
      update(row, item);
    });
  }

  const Board = {
    token: () => localStorage.getItem("dwsPanelToken") || "",
    state: null,
    contacts: {},
    toastTimer: 0,
    aliasesLoaded: false,
    timer: 0,

    headers() {
      const token = Board.token();
      return token ? { "X-Operator-Token": token } : {};
    },

    contactName(id) {
      return Board.contacts[id] || (id ? String(id) : "?");
    },

    avatar(id) {
      const el = node("span", "dws-avatar");
      el.textContent = Array.from(Board.contactName(id).trim())[0] || "?";
      el.title = Board.contactName(id);
      return el;
    },

    toast(message, isError) {
      let el = document.getElementById("dwsToast");
      if (!el) {
        el = node("div", "dws-toast");
        el.id = "dwsToast";
        document.body.append(el);
      }
      el.textContent = message;
      el.classList.toggle("is-error", !!isError);
      el.hidden = false;
      clearTimeout(Board.toastTimer);
      Board.toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
    },

    async post(url, body) {
      const response = await fetch(url, {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json", ...Board.headers() },
        body: JSON.stringify(body || {})
      });
      let payload = {};
      try { payload = await response.json(); } catch (error) { /* 空响应按成功处理 */ }
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      return payload;
    },

    /* ---------- panels ---------- */

    mount(root) {
      if (!root) return;
      clear(root);
      root.classList.add("dws-board");

      const alert = node("div", "dws-board-alert");
      alert.hidden = true;
      alert.append(node("span", "", "面板没有拿到授权，黑板区读不到。"));
      const retry = node("a", "", "重新连接");
      retry.href = "/";
      alert.append(retry);

      const verdicts = node("section", "dws-panel");
      verdicts.hidden = true;
      verdicts.id = "dwsVerdicts";
      const vHead = node("div", "dws-panel-head");
      const vTitle = node("div", "dws-panel-title");
      vTitle.append(node("p", "dws-panel-code", "AWAITING VERDICT / 01"), node("h2", "", "等我裁决"));
      vHead.append(vTitle, node("span", "dws-hint", "看「要你决定」，✓ 同意或写一句话"));
      verdicts.append(vHead, node("div", "dws-verdict-rows"));
      verdicts.lastChild.id = "dwsVerdictRows";

      const runs = node("section", "dws-panel");
      const rHead = node("div", "dws-panel-head");
      const rTitle = node("div", "dws-panel-title");
      rTitle.append(node("p", "dws-panel-code", "ACTIVE RUNS / 02"), node("h2", "", "在跑会话"));
      const rCount = node("span", "dws-count");
      rHead.append(rTitle, rCount);
      const runRows = node("div", "dws-run-rows");
      const orphanRows = node("div", "dws-run-rows");
      runs.append(rHead, runRows, orphanRows);

      const tasks = node("section", "dws-panel");
      const tHead = node("div", "dws-panel-head");
      const tTitle = node("div", "dws-panel-title");
      tTitle.append(node("p", "dws-panel-code", "TASK BOARD / 03"), node("h2", "", "任务"));
      tHead.append(tTitle, node("span", "dws-hint", "点开看事实与相关会话"));
      const cols = node("div", "dws-board-cols");
      // 只留两列在办任务。已完成的量最大，摊成第三列会把面板拉到几屏长，
      // 统一收进下面的折叠区，需要查历史时再展开。
      const colMeta = [["open", "进行中"], ["wait", "等待外部"]];
      const colBoxes = {};
      colMeta.forEach(([key, label]) => {
        const col = node("div", "dws-col");
        const head = node("div", "dws-col-head");
        const count = node("b", "", "0");
        head.append(node("span", "", label), count);
        const cards = node("div", "dws-cards");
        col.append(head, cards);
        colBoxes[key] = { cards, count };
        cols.append(col);
      });
      const doneStrip = node("details", "dws-done-strip");
      doneStrip.hidden = true;
      const doneSummary = node("summary");
      doneSummary.append(icon("chevron"), node("span", "", "已完成"));
      const doneCount = node("b", "", "0");
      doneSummary.append(doneCount);
      const doneCards = node("div", "dws-cards");
      doneStrip.append(doneSummary, doneCards);
      tasks.append(tHead, cols, doneStrip);

      root.append(alert, verdicts, runs, tasks);

      root.append(Board.drawer(), Board.newSession());

      // 事件委托：按钮在 reconcile 里反复替换，逐次绑定会漏。
      root.addEventListener("click", event => {
        const card = event.target.closest("[data-dws-task]");
        if (card) { Board.openDrawer(card.dataset.dwsTask); return; }
        const toggle = event.target.closest("[data-dws-progress]");
        if (toggle) {
          Board.toggleLive(toggle.closest(".dws-run"));
          return;
        }
        const liveToggle = event.target.closest("[data-dws-live-expand]");
        if (liveToggle) {
          Board.toggleLive(liveToggle.closest(".dws-run"));
          return;
        }
        const goal = event.target.closest("[data-dws-expand]");
        if (goal) { goal.closest(".dws-verdict").classList.toggle("open"); return; }
        const steerOpen = event.target.closest("[data-dws-steer]");
        if (steerOpen) {
          const box = steerOpen.closest(".dws-run").querySelector(".dws-steer");
          box.hidden = !box.hidden;
          if (!box.hidden) box.querySelector("input").focus();
          return;
        }
        const answer = event.target.closest("[data-dws-answer]");
        if (answer) { Board.answer(answer); return; }
        const steer = event.target.closest("[data-dws-send-steer]");
        if (steer) { Board.sendSteer(steer); return; }
      });

      root.addEventListener("keydown", event => {
        if (event.key !== "Enter") return;
        const answerField = event.target.closest("[data-dws-answer-input]");
        if (answerField) {
          Board.answer(answerField.closest(".dws-verdict").querySelector("[data-dws-answer]"), answerField.value);
        }
        const steer = event.target.closest("[data-dws-steer-input]");
        if (steer) { Board.sendSteer(steer.closest(".dws-run").querySelector("[data-dws-send-steer]"), steer.value); }
      });

      document.getElementById("dwsDrawerClose")?.addEventListener("click", Board.closeDrawer);
      document.getElementById("dwsDrawerBackdrop")?.addEventListener("click", Board.closeDrawer);
      document.getElementById("dwsNewCancel")?.addEventListener("click", Board.closeNewSession);
      document.getElementById("dwsNewSend")?.addEventListener("click", Board.sendNewSession);
      document.addEventListener("keydown", event => {
        if (event.key !== "Escape") return;
        if (!document.getElementById("dwsNewOverlay").hidden) Board.closeNewSession();
        else if (!document.getElementById("dwsDrawer").hidden) Board.closeDrawer();
      });

      Board.doneStrip = { root: doneStrip, count: doneCount, cards: doneCards };
      Board.colBoxes = colBoxes;
      Board.runRows = runRows;
      Board.orphanRows = orphanRows;
      Board.verdicts = verdicts;
      Board.verdictRows = document.getElementById("dwsVerdictRows");
      Board.runningCount = rCount;

      setInterval(() => {
        document.querySelectorAll(".dws-run-elapsed[data-start]").forEach(el => {
          el.textContent = elapsed(el.dataset.start);
        });
      }, 1000);
    },

    drawer() {
      const backdrop = node("div", "dws-drawer-backdrop");
      backdrop.id = "dwsDrawerBackdrop";
      backdrop.hidden = true;
      const aside = node("aside", "dws-drawer");
      aside.id = "dwsDrawer";
      aside.hidden = true;
      aside.setAttribute("aria-label", "任务详情");
      const head = node("div", "dws-drawer-head");
      const goal = node("div", "dws-drawer-goal");
      goal.id = "dwsDrawerGoal";
      const close = node("button", "dws-icon-btn");
      close.type = "button";
      close.id = "dwsDrawerClose";
      close.setAttribute("aria-label", "关闭");
      close.append(icon("close"));
      head.append(goal, close);
      const people = node("div", "dws-drawer-people");
      people.id = "dwsDrawerPeople";
      const facts = node("div", "dws-facts");
      facts.id = "dwsDrawerFacts";
      aside.append(head, people, facts);
      const wrap = document.createDocumentFragment();
      wrap.append(backdrop, aside);
      return wrap;
    },

    newSession() {
      const overlay = node("div", "dws-modal-overlay");
      overlay.id = "dwsNewOverlay";
      overlay.hidden = true;
      const card = node("div", "dws-modal");
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-label", "开启会话");
      const text = node("textarea");
      text.id = "dwsNewText";
      text.rows = 4;
      text.placeholder = "要它做什么？";
      const select = node("select");
      select.id = "dwsNewContact";
      select.setAttribute("aria-label", "联系人");
      const actions = node("div", "dws-modal-actions");
      const error = node("span", "dws-modal-error");
      error.id = "dwsNewError";
      error.hidden = true;
      const cancel = node("button", "dws-btn", "取消");
      cancel.type = "button";
      cancel.id = "dwsNewCancel";
      const send = node("button", "dws-btn primary", "发送");
      send.type = "button";
      send.id = "dwsNewSend";
      actions.append(error, cancel, send);
      card.append(text, select, actions);
      overlay.append(card);
      return overlay;
    },

    /* ---------- render ---------- */

    render(data) {
      Board.state = data;
      Board.renderVerdicts(data);
      Board.renderRunning(data);
      Board.renderTasks(data);
    },

    /* 裁决卡要说清三件事：在等什么、要你决定什么、已知事实是什么。
       只贴 goal 等于让人自己猜——goal 是"问题是什么"，不是"请你做什么"。 */
    renderVerdicts(data) {
      const items = list(data.waiting_operator);
      Board.verdicts.hidden = !items.length;
      if (!items.length) return;
      reconcile(Board.verdictRows, items, task => String(task.id),
        task => {
          const row = node("article", "dws-verdict");
          row.append(Board.avatar(task.contact_id));

          const body = node("div", "dws-verdict-body");
          const toggle = node("button", "dws-verdict-goal-button");
          toggle.type = "button";
          toggle.dataset.dwsExpand = "1";
          toggle.setAttribute("aria-label", "展开或收起详情");
          toggle.append(node("div", "dws-verdict-goal"));
          const ask = node("p", "dws-verdict-ask");
          const facts = node("ul", "dws-verdict-facts");
          const meta = node("div", "dws-verdict-meta");
          // 选项按钮 + 自定义输入：绝大多数裁决是"批准/驳回"，点一下就完；
          // 需要细说时再落到底下的输入框。
          const choices = node("div", "dws-choices");
          body.append(toggle, ask, facts, meta, choices);
          const actions = node("div", "dws-verdict-actions");
          const input = node("input", "dws-answer");
          input.type = "text";
          input.placeholder = "或者，自己写一句";
          input.dataset.dwsAnswerInput = "1";
          const send = node("button", "dws-icon-btn");
          send.type = "button";
          send.title = "送出这句话";
          send.setAttribute("aria-label", "送出");
          send.dataset.dwsAnswer = "";
          send.append(icon("send"));
          actions.append(input, send);
          row.append(body, actions);

          row._goal = toggle.firstChild;
          row._ask = ask;
          row._facts = facts;
          row._meta = meta;
          row._choices = choices;
          return row;
        },
        (row, task) => {
          const goal = task.goal || "";
          row._goal.textContent = goal;

          // "要你决定什么"：优先用证据摘要里可执行的最后一句，没有就退回等待对象。
          const proposal = Board.verdictProposal(task);
          row._ask.textContent = proposal;
          row._ask.hidden = !proposal;

          // 选项：点一下就提交，不必打字。自定义输入仍在下面。
          clear(row._choices);
          Board.verdictOptions(task, proposal).forEach(option => {
            const button = node("button", `dws-choice ${option.tone || ""}`.trim(), option.label);
            button.type = "button";
            button.addEventListener("click", () => Board.answer(button, option.answer));
            row._choices.append(button);
          });

          const facts = list(task.recent_facts ?? task.facts).slice(-3);
          clear(row._facts);
          facts.forEach(fact => {
            const item = node("li");
            item.append(node("span", "dws-fact-time", clock(fact.created_at)),
                        node("span", "dws-fact-text", fact.text || ""));
            row._facts.append(item);
          });
          row._facts.hidden = !facts.length;

          row._meta.textContent = "";
          if (task.waiting_on) row._meta.append(node("span", "", `在等 ${task.waiting_on}`));
          if (task.last_evidence_at) row._meta.append(node("span", "", `证据 ${clock(task.last_evidence_at)}`));
          const resources = list(task.resources);
          if (resources.length) row._meta.append(node("span", "", resources.join(" · ")));
          row.querySelector("[data-dws-answer='']").dataset.dwsTaskId = String(task.id);
          row._taskId = String(task.id);
        });
    },

    /* 裁决选项。默认给"同意 / 不同意"，问题里出现具体动作时把动作写进
       选项文案，让人一眼知道点下去会发生什么；也可以完全自己写。 */
    verdictOptions(task, proposal) {
      const text = `${proposal} ${task.goal || ""}`;
      const options = [];
      if (/批准|同意|确认|补登记|执行|上线|发布|切到|回滚|重启/.test(text)) {
        options.push({ label: `同意，${Board.optionVerb(text)}`, answer: "同意", tone: "ok" });
        options.push({ label: "先别动，我再看看", answer: "先别动，我再看看", tone: "" });
      } else {
        options.push({ label: "同意", answer: "同意", tone: "ok" });
        options.push({ label: "不同意", answer: "不同意", tone: "" });
      }
      return options;
    },

    optionVerb(text) {
      const match = text.match(/(补登记|执行|上线发布|上线|发布|回滚|重启|切到[^，。；]{0,20}|确认[^，。；]{0,12})/);
      return match ? match[1] : "按上面说的做";
    },

    /* 要你拍板的那件事。
       1) task.ask —— 模型写 waiting_on 时必须显式给的提问，最可靠；
       2) 摘要里明确写"待…确认/裁决/拍板"的句子；
       3) 最近一条 agent 事实（通常就是"已提交裁决"的具体动作）；
       4) 摘要最后一句，聊胜于无。 */
    verdictProposal(task) {
      const ask = String(task.ask || "").trim();
      if (ask) return ask;
      const summary = String(task.evidence_summary || "").trim();
      const sentences = summary.split(/(?<=[。；;\n])/).map(part => part.trim()).filter(Boolean);
      const pending = /(待|等|请|需|要)[^。；]{0,24}(确认|裁决|决定|拍板|批准|同意)/;
      const hit = sentences.find(sentence => pending.test(sentence));
      if (hit) return hit;

      const facts = list(task.recent_facts ?? task.facts);
      const latest = facts[facts.length - 1];
      if (latest && String(latest.text || "").trim()) return String(latest.text).trim();

      return sentences.length ? sentences[sentences.length - 1] : "";
    },

    answer(button, forced) {
      const row = button.closest(".dws-verdict");
      const input = row.querySelector(".dws-answer");
      const value = (forced !== undefined ? forced : input.value).trim();
      if (!value) { Board.toast("先写一句，或点一个选项", true); return; }
      const taskId = button.dataset.dwsTaskId || row._taskId;
      button.disabled = true;
      Board.post("/api/task/answer", { task_id: taskId, answer: value })
        .then(() => {
          if (input) input.value = "";
          Board.toast("已答复");
          Board.refresh();
        })
        .catch(error => { Board.toast(error.message, true); button.disabled = false; });
    },

    /* 实时流：默认只留一眼能扫的摘要——
       think / tool 各压一行（tool 只给名字），正文完整显示；点开才铺完整轨迹。
       正文一旦落定（最后一条已封口），自动只留正文，轨迹收进展开里。 */
    renderLive(row, live, progress) {
      const expanded = row._liveExpanded === true;
      const finalBody = Board.finalBody(live);
      // 折叠时：有定稿正文就只显示它；否则显示最近几条一行摘要。
      const shown = expanded ? live : (finalBody ? [finalBody] : live.slice(-6));
      const visible = progress.length && expanded ? progress : [];
      const key = [
        expanded ? 1 : 0,
        finalBody ? 1 : 0,
        shown.map(item => `${item.seq}:${(item.text || "").length}:${item.finished ? 1 : 0}`).join("|"),
        visible.map(item => `${item.created_at}|${item.text || ""}`).join("\n")
      ].join("##");
      if (row._liveKey === key) return;
      const follow = row._steps.scrollHeight - row._steps.scrollTop - row._steps.clientHeight < 40;
      row._liveKey = key;
      clear(row._steps);
      row._steps.classList.toggle("is-compact", !expanded);
      shown.forEach(entry => row._steps.append(Board.liveRow(entry, expanded)));

      if (!expanded) {
        const hiddenRows = live.length - shown.length;
        if (hiddenRows > 0 || progress.length) {
          const more = node("div", "dws-live-more");
          more.dataset.dwsLiveExpand = "1";
          more.append(node("span", "", hiddenRows > 0 ? `另有 ${hiddenRows} 条` : "里程碑"),
                      node("b", "", "展开看完整轨迹"));
          row._steps.append(more);
        }
      } else if (progress.length) {
        row._steps.append(node("div", "dws-live-sep", "里程碑"));
        progress.forEach(item => {
          const line = node("div", "dws-progress-row");
          line.append(node("span", "dws-progress-time", clock(item.created_at)),
                      node("span", "dws-progress-text", item.text || ""));
          row._steps.append(line);
        });
      }
      // 有内容且原本在底部时才自动跟随，免得用户往回翻时被拽回去。
      if (follow) row._steps.scrollTop = row._steps.scrollHeight;
    },

    /* 定稿正文：最后一条是已封口的 message，就认为这一轮的答卷出来了。
       模型要是继续调工具，就会多出新条目，这里自然退回摘要状态。 */
    finalBody(live) {
      const last = live[live.length - 1];
      if (!last || last.kind !== "message") return null;
      if (last.streaming) return null;
      return String(last.text || "").trim() ? last : null;
    },

    /* 展开/收起完整轨迹。折叠只影响渲染，不改缓冲。 */
    toggleLive(rowBox) {
      const box = rowBox.querySelector(".dws-live");
      if (!box) return;
      rowBox._liveExpanded = !rowBox._liveExpanded;
      rowBox._liveKey = "";
      const toggle = rowBox.querySelector("[data-dws-progress]");
      if (toggle) toggle.setAttribute("aria-expanded", String(rowBox._liveExpanded));
      Board.renderLive(rowBox, list(rowBox._liveData), list(rowBox._progressData));
    },

    liveRow(entry, expanded) {
      const kind = entry.kind || "message";
      const row = node("div", `dws-live-row is-${kind}`);
      const tag = node("span", "dws-live-tag", {
        think: "思考", message: "正文", tool: "工具", tool_result: "工具"
      }[kind] || kind);
      const body = node("span", "dws-live-text");
      if (kind === "tool") {
        // 折叠时只留工具名：参数是排障细节，属于"完整轨迹"。
        body.append(node("b", "dws-live-tool", entry.name || "tool"));
        if (entry.finished) {
          body.append(node("span", `dws-live-status ${entry.failed ? "is-failed" : "is-ok"}`,
                           entry.failed ? "失败" : "完成"));
        }
        if (expanded && entry.text) body.append(node("span", "dws-live-args", entry.text));
      } else if (expanded) {
        body.textContent = entry.text || "";
      } else {
        body.textContent = entry.text || "";
      }
      row.append(tag, body);
      if (expanded) row.append(node("span", "dws-live-time", clock(entry.at)));
      return row;
    },

    renderRunning(data) {
      const runs = list(data.runs).filter(run => run.status === "running" || run.status === "waiting");
      runs.sort((a, b) => String(a.started_at).localeCompare(String(b.started_at)));
      Board.runningCount.textContent = runs.length ? `${runs.length} 个` : "";
      // 占位必须放在 reconcile 之后：它没有 data-key，会被当成待删元素清掉，
      // 面板就变成一片空白，看不出是"没有会话"还是"加载失败"。
      const emptyRun = node("div", "dws-progress-empty", "现在没有在跑的会话");
      const existingEmpty = Board.runRows.querySelector(".dws-progress-empty");
      if (existingEmpty) existingEmpty.remove();

      reconcile(Board.runRows, runs, run => String(run.session_id),
        run => {
          const row = node("article", "dws-run");
          row.append(Board.avatar(run.contact_id));
          const main = node("div", "dws-run-main");
          const line = node("div", "dws-run-line");
          const contact = node("span", "dws-run-contact");
          const headline = node("span", "dws-run-headline");
          const time = node("span", "dws-run-elapsed");
          line.append(contact, headline, time);
          const meta = node("div", "dws-run-meta");
          // 九段阶段条沿用原设计的 .steps/.step 结构：每段一条线 + 只在
          // 当前段露标签。没有里程碑时它就是唯一"现在跑到哪"的依据。
          const rail = node("div", "dws-rail");
          const steps = node("div", "steps");
          const stepEls = PHASES.map(([key, label]) => {
            const step = node("div", "step");
            step.append(node("div", "step-line"), node("div", "step-label", label));
            steps.append(step);
            return step;
          });
          rail.append(steps);
          rail.hidden = true;
          const toggle = node("button", "dws-progress-toggle");
          toggle.type = "button";
          toggle.dataset.dwsProgress = "1";
          toggle.setAttribute("aria-expanded", "false");
          toggle.append(icon("chevron", "dws-icon"), node("span", "dws-progress-label", "轨迹"));
          // 默认折叠：只留"思考一行 / 工具一行 / 正文全文"，展开才铺完整轨迹。
          const stepList = node("div", "dws-live is-compact");
          stepList.hidden = false;
          const steerBox = node("div", "dws-steer");
          steerBox.hidden = true;
          const steerInput = node("input", "dws-answer");
          steerInput.type = "text";
          steerInput.placeholder = "对它说";
          steerInput.dataset.dwsSteerInput = "1";
          const steerSend = node("button", "dws-icon-btn");
          steerSend.type = "button";
          steerSend.title = "送出";
          steerSend.setAttribute("aria-label", "送出");
          steerSend.dataset.dwsSendSteer = "1";
          steerSend.append(icon("send"));
          steerBox.append(steerInput, steerSend);
          main.append(line, meta, rail, toggle, stepList, steerBox);
          const chat = node("button", "dws-icon-btn");
          chat.type = "button";
          chat.title = "介入";
          chat.dataset.dwsSteer = "1";
          chat.setAttribute("aria-label", "介入");
          chat.append(icon("chat"));
          row.append(main, chat);
          row._contact = contact;
          row._headline = headline;
          row._time = time;
          row._meta = meta;
          row._rail = rail;
          row._stepEls = stepEls;
          row._toggle = toggle;
          row._steps = stepList;
          row._steerSend = steerSend;
          row._steerBox = steerBox;
          row._steerChat = chat;
          return row;
        },
        (row, run) => {
          const progress = list(run.recent_progress ?? run.progress);
          const live = list(run.live);
          row._contact.textContent = Board.contactName(run.contact_id);
          const headline = run.headline || "";
          const text = progress.length ? headline : (headline ? `收到：${headline}` : "已开始处理");
          if (row._headline.textContent !== text) row._headline.textContent = text;
          row._headline.title = headline;
          row._headline.classList.toggle("placeholder", !progress.length);
          row._time.dataset.start = run.started_at || "";
          row._time.textContent = elapsed(run.started_at);
          row._time.className = "dws-run-elapsed";
          row.classList.toggle("is-waiting", run.status === "waiting");

          const index = Math.max(0, PHASES.findIndex(([key]) => key === run.phase));
          row._meta.textContent = "";
          if (run.status === "waiting") row._meta.append(node("span", "", "等外部条件"));
          if (run.recent_progress && run.recent_progress.length > 0) {
            row._meta.append(node("span", "", `${run.recent_progress.length} 条里程碑`));
          }

          // 阶段条：即使模型没报里程碑，也要能看出跑到第几步。
          const hasPhase = Boolean(run.phase) && PHASES[index][0] === run.phase;
          row._rail.hidden = !hasPhase;
          if (hasPhase) {
            const railKey = `${run.phase}`;
            if (row._railKey !== railKey) {
              row._railKey = railKey;
              row._stepEls.forEach((step, stepIndex) => {
                step.className = `step ${stepIndex < index ? "done" : stepIndex === index ? "current" : ""}`.trim();
              });
            }
          }

          // 折叠时也要能看到"它在动"，所以有轨迹就露摘要条。
          row._toggle.hidden = !progress.length && !live.length;
          row._steps.hidden = !progress.length && !live.length;
          row._liveData = live;
          row._progressData = progress;
          Board.renderLive(row, live, progress);
          row._steerSend.dataset.dwsSession = String(run.session_id);
          // 协议级 steer 能力由后端 run.steerable 声明；不可介入的
          // 协议（如 dsh headless 一次性运行）整个按钮藏掉，而不是
          // 点了才报"会话不在可介入状态"。
          const canSteer = run.steerable !== false;
          row._steerChat.hidden = !canSteer;
          if (!canSteer) { row._steerBox.hidden = true; }
        });

      if (!runs.length) Board.runRows.append(emptyRun);

      const known = new Set(runs.map(run => run.session_id));
      const orphans = list(data.orphan_runs).filter(item => !known.has(item.session_id));
      reconcile(Board.orphanRows, orphans, item => String(item.session_id),
        () => node("article", "dws-run"),
        (row, item) => {
          clear(row);
          row.append(Board.avatar(item.contact_id));
          const main = node("div", "dws-run-main");
          main.append(node("div", "dws-run-headline", "会话进程还在，黑板登记已收口"));
          row.append(main);
        });
    },

    renderTasks(data) {
      const tasks = list(data.tasks);
      const mine = new Set(list(data.waiting_operator).map(task => String(task.id)));
      const groups = { open: [], wait: [], done: [] };
      tasks.forEach(task => {
        const key = task.status === "open" ? "open" : task.status === "waiting" ? "wait" : "done";
        groups[key].push(task);
      });
      groups.done.sort((a, b) => (a.status === "cancelled") - (b.status === "cancelled")
        || String(b.updated_at || "").localeCompare(String(a.updated_at || "")));

      const makeCard = () => {
        const card = node("button", "dws-card");
        card.type = "button";
        card.append(Board.avatar(null));
        const goal = node("span", "dws-card-goal");
        card.append(goal);
        card._goal = goal;
        card._avatar = card.firstChild;
        return card;
      };
      const update = (card, task) => {
        card._goal.textContent = task.goal || "";
        card._goal.title = task.goal || "";
        const avatar = Board.avatar(task.contact_id);
        card.replaceChild(avatar, card._avatar);
        card._avatar = avatar;
        card.dataset.dwsTask = String(task.id);
        card.classList.toggle("is-done", task.status !== "open" && task.status !== "waiting");
        card.classList.toggle("is-waiting", task.status === "waiting");
        card.classList.toggle("is-cancelled", task.status === "cancelled");
        card.classList.toggle("is-mine", mine.has(String(task.id)));
      };

      Object.entries(groups).forEach(([key, items]) => {
        const box = Board.colBoxes[key];
        if (!box) return;  // done 已改为折叠区，不再是列
        reconcile(box.cards, items, task => String(task.id), makeCard, update);
        box.count.textContent = String(items.length);
        const empty = box.cards.querySelector(".dws-col-empty");
        if (!items.length && !empty) box.cards.append(node("div", "dws-col-empty", "没有进行中的任务"));
        if (items.length && empty) empty.remove();
      });
      // 进行中为空时把等待列铺满：两列都空着一半更难看。
      const cols = Board.colBoxes.wait.cards.closest(".dws-board-cols");
      if (cols) cols.classList.toggle("single", !groups.open.length);

      // 折叠区展示全部已完成（含取消），默认收起；数量放在摘要上。
      const done = groups.done;
      Board.doneStrip.root.hidden = !done.length;
      Board.doneStrip.count.textContent = done.length ? `${done.length} 个` : "";
      if (done.length) {
        reconcile(Board.doneStrip.cards, done, task => String(task.id), makeCard, update);
      }
    },

    /* ---------- 交互 ---------- */

    openDrawer(taskId) {
      const task = list(Board.state?.tasks).find(item => String(item.id) === String(taskId));
      if (!task) return;
      document.getElementById("dwsDrawerGoal").textContent = task.goal || "";
      const people = document.getElementById("dwsDrawerPeople");
      clear(people);
      [...new Set([task.contact_id, ...list(task.contact_ids)])].filter(Boolean)
        .forEach(id => people.append(Board.avatar(id)));
      const facts = document.getElementById("dwsDrawerFacts");
      clear(facts);
      const entries = list(task.facts ?? task.recent_facts);
      entries.forEach(fact => {
        const row = node("div", "dws-fact");
        const dot = node("span", "dws-fact-dot");
        dot.classList.add(fact.source === "human" ? "is-human" : fact.source === "contact" ? "is-contact" : "is-agent");
        row.append(dot, node("span", "dws-fact-text", fact.text || ""), node("span", "dws-fact-time", clock(fact.created_at)));
        facts.append(row);
      });
      if (!entries.length) facts.append(node("div", "dws-progress-empty", "还没有留下事实"));
      document.getElementById("dwsDrawerBackdrop").hidden = false;
      document.getElementById("dwsDrawer").hidden = false;
    },

    closeDrawer() {
      document.getElementById("dwsDrawerBackdrop").hidden = true;
      document.getElementById("dwsDrawer").hidden = true;
    },

    openNewSession() {
      const overlay = document.getElementById("dwsNewOverlay");
      overlay.hidden = false;
      document.getElementById("dwsNewError").hidden = true;
      const select = document.getElementById("dwsNewContact");
      if (!Board.aliasesLoaded) {
        Board.aliasesLoaded = true;
        clear(select);
        select.append(new Option("内部任务", ""));
        fetch("/api/config", { cache: "no-store" })
          .then(response => response.ok ? response.json() : { contacts: [] })
          .then(data => {
            clear(select);
            select.append(new Option("内部任务", ""));
            list(data.contacts).forEach(contact =>
              select.append(new Option(contact.display_name || contact.alias, contact.alias)));
          })
          .catch(() => {});
      }
      document.getElementById("dwsNewText").focus();
    },

    closeNewSession() {
      document.getElementById("dwsNewOverlay").hidden = true;
      document.getElementById("dwsNewText").value = "";
    },

    sendNewSession() {
      const textEl = document.getElementById("dwsNewText");
      const error = document.getElementById("dwsNewError");
      const text = textEl.value.trim();
      if (!text) { error.textContent = "请输入内容"; error.hidden = false; return; }
      const button = document.getElementById("dwsNewSend");
      button.disabled = true;
      Board.post("/api/session/new", {
        text,
        contact_alias: document.getElementById("dwsNewContact").value || null
      })
        .then(data => {
          Board.closeNewSession();
          Board.toast(`已开启 ${String(data.sessionId || "").slice(0, 8)}`);
          Board.refresh();
        })
        .catch(exc => { error.textContent = exc.message; error.hidden = false; })
        .finally(() => { button.disabled = false; });
    },

    sendSteer(button, forced) {
      const value = (forced !== undefined ? forced : button.closest(".dws-steer").querySelector("input").value).trim();
      if (!value) return;
      button.disabled = true;
      Board.post(`/api/run/${encodeURIComponent(button.dataset.dwsSession)}/steer`, { text: value })
        .then(() => {
          const box = button.closest(".dws-steer");
          box.hidden = true;
          box.querySelector("input").value = "";
          Board.refresh();
        })
        .catch(error => { Board.toast(error.message, true); button.disabled = false; });
    },

    /* ---------- 铃 ---------- */

    mountBell(button) {
      if (!button) return;
      clear(button);
      button.append(icon("bell"));
      const badge = node("span", "dws-bell-badge");
      badge.hidden = true;
      button.append(badge);
      const menu = node("div", "dws-bell-menu");
      menu.hidden = true;
      const wrap = button.parentElement;
      if (wrap) {
        wrap.classList.add("dws-bell-wrap");
        wrap.append(menu);
      }
      button.addEventListener("click", event => {
        event.stopPropagation();
        menu.hidden = !menu.hidden;
        if (!menu.hidden) Board.renderBell(menu, badge);
      });
      document.addEventListener("click", event => {
        if (menu.hidden) return;
        if (wrap && wrap.contains(event.target)) return;
        menu.hidden = true;
      });
      Board.bellMenu = menu;
      Board.bellBadge = badge;
    },

    renderBell(menu, badge) {
      const data = Board.state || {};
      const waiting = list(data.waiting_operator);
      const failed = [];
      const seen = new Set();
      [...list(data.runs), ...list(data.recent_done), ...list(data.recent_failed)].forEach(run => {
        if (run.status !== "failed" && run.outcome !== "failed") return;
        if (seen.has(run.session_id)) return;
        seen.add(run.session_id);
        if (within24h(run.ended_at || run.updated_at)) failed.push(run);
      });
      const total = waiting.length + failed.length;
      badge.textContent = String(total);
      badge.hidden = total === 0;
      if (menu.hidden) return;
      clear(menu);
      if (!total) { menu.append(node("div", "dws-bell-empty", "没有需要你处理的事")); return; }
      waiting.forEach(task => {
        const row = node("button", "dws-bell-row is-mine");
        row.type = "button";
        row.append(Board.avatar(task.contact_id), node("span", "dws-bell-text", task.goal || ""));
        row.addEventListener("click", () => {
          menu.hidden = true;
          document.getElementById("dwsVerdicts")?.scrollIntoView({ behavior: "smooth", block: "start" });
        });
        menu.append(row);
      });
      failed.forEach(run => {
        const row = node("button", "dws-bell-row");
        row.type = "button";
        row.append(Board.avatar(run.contact_id), node("span", "dws-bell-text", `失败：${run.headline || run.goal || "未记录原因"}`));
        row.addEventListener("click", () => {
          menu.hidden = true;
          if (run.task_id) Board.openDrawer(run.task_id);
        });
        menu.append(row);
      });
    },

    /* ---------- 拉取 ---------- */

    async refresh() {
      const alert = document.querySelector(".dws-board-alert");
      try {
        const response = await fetch("/api/board", { cache: "no-store", headers: Board.headers() });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        Board.render(data);
        if (Board.bellMenu && !Board.bellMenu.hidden) Board.renderBell(Board.bellMenu, Board.bellBadge);
        else if (Board.bellBadge) {
          const total = list(data.waiting_operator).length
            + [...list(data.runs), ...list(data.recent_done), ...list(data.recent_failed)]
              .filter(run => (run.status === "failed" || run.outcome === "failed") && within24h(run.ended_at || run.updated_at))
              .length;
          Board.bellBadge.textContent = String(total);
          Board.bellBadge.hidden = total === 0;
        }
        if (alert) alert.hidden = true;
      } catch (error) {
        if (alert) alert.hidden = false;
      }
    },

    async loadContacts() {
      try {
        const response = await fetch("/api/config", { cache: "no-store" });
        if (!response.ok) return;
        const data = await response.json();
        Board.contacts = {};
        list(data.contacts).forEach(contact => {
          if (contact.user_id) Board.contacts[contact.user_id] = contact.display_name || contact.alias;
          if (contact.alias) Board.contacts[contact.alias] = contact.display_name || contact.alias;
        });
      } catch (error) { /* 拿不到名字时退回 id 显示 */ }
    },

    async start(options) {
      const config = options || {};
      if (config.board) Board.mount(config.board);
      if (config.bell) Board.mountBell(config.bell);
      if (config.newSession) {
        clear(config.newSession);
        config.newSession.append(icon("plus"));
        config.newSession.addEventListener("click", Board.openNewSession);
      }
      const urlToken = new URLSearchParams(location.search).get("token");
      if (urlToken) localStorage.setItem("dwsPanelToken", urlToken);
      await Board.loadContacts();
      await Board.refresh();
      clearInterval(Board.timer);
      Board.timer = setInterval(Board.refresh, 2000);
    }
  };

  window.DwsBoard = Board;
})();
