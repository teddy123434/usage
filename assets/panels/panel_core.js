    const defaultPanelHooks = {
      accentFallback(cardEl, computedAccent) {
        return computedAccent;
      },
      applyFillVisual(fillEl, { available, percent, color }) {
        fillEl.style.width = available ? `${Math.max(0, Math.min(100, percent))}%` : "0%";
        fillEl.style.setProperty("--fill-color", color);
      },
      projectRankLabels: ["1", "2", "3"],
      renderProjectRow(p, index, ranks) {
        const row = document.createElement("div");
        row.className = "proj-row";
        const rank = document.createElement("span");
        rank.className = "proj-rank";
        rank.textContent = ranks[index];
        const name = document.createElement("span");
        name.className = "proj-name";
        name.textContent = p.name || "";
        const tok = document.createElement("span");
        tok.className = "proj-tokens";
        tok.textContent = p.tokensText || "";
        const cost = document.createElement("span");
        cost.className = "proj-cost";
        cost.textContent = p.costText || "";
        const bar = document.createElement("div");
        bar.className = "proj-bar";
        const fill = document.createElement("div");
        fill.className = "proj-bar-fill";
        fill.style.width = `${p.sharePercent}%`;
        bar.append(fill);
        row.append(rank, name, tok, cost, bar);
        return row;
      },
      switchButtonStrategy(state) {
        // The switch button lives in the Claude card header; when that card is
        // hidden, move it to the next visible home so the menu stays reachable.
        const button = document.querySelector('[data-action="switch"]');
        if (!button) return;
        let host = document.querySelector('[data-card="claude"] .brand');
        let className = "switch";
        if (state.hideClaude && !state.hideCodex) {
          host = document.querySelector('[data-card="codex"] .brand');
        } else if (state.hideClaude && state.hideCodex && !state.hideAgy) {
          host = document.querySelector('[data-card="agy"] .brand');
        } else if (state.hideClaude && state.hideCodex && state.hideAgy && !state.hideGrok) {
          host = document.querySelector('[data-card="grok"] .brand');
        } else if (state.hideClaude && state.hideCodex && state.hideAgy && state.hideGrok && !state.hideOpenRouter) {
          host = document.querySelector('[data-card="openrouter"] .brand, [data-card="openrouter"] .header');
        }
        if (!host || (state.hideClaude && state.hideCodex && state.hideAgy && state.hideGrok && state.hideOpenRouter)) {
          host = document.querySelector(".footer .actions");
          className = "action";
        }
        if (!host) return;
        button.className = className;
        if (button.parentElement !== host) host.appendChild(button);
      },
      pointerdownExcludeSelector: "button, a, .codex-stale-info",
    };

    if (!window.PanelHooks || typeof window.PanelHooks !== "object") {
      window.PanelHooks = {};
    }
    Object.entries(defaultPanelHooks).forEach(([name, defaultValue]) => {
      if (window.PanelHooks[name] === undefined) {
        window.PanelHooks[name] = defaultValue;
      }
    });

    const I18N = {{I18N_BUNDLE}};
    const FALLBACK_LANGUAGE = "en";
    const root = document.documentElement;
    const switchHostStyle = document.createElement("style");
    switchHostStyle.textContent = `
      [data-card="codex"] .usage-switch-host > h1,
      [data-card="codex"] .usage-switch-host > .header-copy {
        flex: 0 0 auto !important;
        min-width: max-content !important;
        overflow: visible !important;
      }
      [data-card="codex"] .usage-switch-host > [data-codex-stale] {
        flex: 1 1 0 !important;
        min-width: 0 !important;
        overflow: hidden !important;
      }
      [data-card="codex"] .usage-switch-host [data-codex-stale-age] {
        display: block;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      [data-card="grok"],
      [data-card="openrouter"] { cursor: grab; }
      .hide-openrouter [data-card="openrouter"] { display: none !important; }
      .usage-source-provenance {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        margin-left: auto;
        min-width: 0;
        font-size: 10px;
        line-height: 1.2;
      }
      .usage-source-badge,
      .usage-source-marker {
        display: inline-block;
        max-width: 130px;
        padding: 2px 6px;
        border: 1px solid currentColor;
        border-radius: 999px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .usage-source-marker { color: var(--warn, #ffc439); }
      .usage-source-marker[data-reauth="true"] { color: var(--danger, #ff453a); }
      .usage-openrouter-headline {
        margin: 8px 0;
        color: var(--accent, currentColor);
        font-size: 24px;
        font-weight: 700;
      }
      .usage-openrouter-details { display: grid; gap: 4px; }
      .usage-openrouter-detail {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        font-size: 12px;
      }
      .usage-openrouter-detail > :first-child { color: var(--muted, currentColor); }
    `;
    document.head.appendChild(switchHostStyle);
    let currentLanguage = "en";
    let projectRange = "1d";
    let latestState = null;

    function languageTable(language) {
      return I18N[language] || I18N[FALLBACK_LANGUAGE] || {};
    }

    function t(key, params = {}) {
      const template = languageTable(currentLanguage)[key] || languageTable(FALLBACK_LANGUAGE)[key] || key;
      return template.replace(/\{(\w+)\}/g, (_, name) => `${params[name] ?? ""}`);
    }

    function labels() {
      return {
        session: t("session_label"),
        weekly: t("weekly_label"),
      };
    }

    function projectRangeLabel(range) {
      if (range === "yesterday") return t("project_range_yesterday");
      if (range === "7d") return t("project_range_7d");
      if (range === "30d") return t("project_range_30d");
      if (range === "all") return t("project_range_all");
      return t("project_range_1d");
    }

    function applyStaticText() {
      document.documentElement.lang = currentLanguage === "zh-TW" ? "zh-Hant" : currentLanguage;
      document.querySelectorAll("[data-i18n]").forEach((node) => {
        const key = node.dataset.i18n;
        if (key) node.textContent = t(key);
      });
      const rangeButton = document.querySelector('[data-action="toggle-project-range"]');
      if (rangeButton) rangeButton.textContent = projectRangeLabel(projectRange);
    }

    window.usageSetLanguage = function usageSetLanguage(language) {
      currentLanguage = I18N[language] ? language : FALLBACK_LANGUAGE;
      applyStaticText();
      if (latestState) {
        window.usageApplyState({ ...latestState, language: currentLanguage });
      }
    };

    function cssVar(name) {
      return getComputedStyle(root).getPropertyValue(name).trim();
    }

    // Panels may draw a critical halo past this; colorFor's 80% only turns text red.
    const CRITICAL_PERCENT = 90;

    function colorFor(percent, fallback) {
      if (typeof percent !== "number") return fallback;
      if (percent >= 80) return cssVar("--danger");
      if (percent >= 50) return cssVar("--warn");
      return fallback;
    }

    function renderRow(card, key, row) {
      const el = document.querySelector(`[data-card="${card}"] [data-row="${key}"]`);
      if (!el) return;
      el.hidden = card === "codex" && !row;
      if (el.hidden) return;
      const cardEl = el.closest(".card");
      if (!cardEl) return;
      const data = row || {
        percent: null,
        percentText: "--",
        resetText: t("reset_placeholder"),
        warning: false,
        available: false,
      };
      const available = data.available === true && typeof data.percent === "number";
      const accent = window.PanelHooks.accentFallback(
        cardEl,
        getComputedStyle(cardEl).getPropertyValue("--accent").trim()
      );
      const color = colorFor(data.percent, accent);
      el.dataset.available = available ? "true" : "false";
      if (el.dataset.rowReady !== "true") {
        const head = document.createElement("div");
        head.className = "row-head";
        const rowTitle = document.createElement("div");
        rowTitle.className = "row-title";
        const rowPercent = document.createElement("div");
        rowPercent.className = "percent";
        head.append(rowTitle, rowPercent);
        const track = document.createElement("div");
        track.className = "track";
        const rowFill = document.createElement("div");
        rowFill.className = "fill";
        track.append(rowFill);
        const rowReset = document.createElement("div");
        rowReset.className = "reset";
        el.replaceChildren(head, track, rowReset);
        el.dataset.rowReady = "true";
      }
      const title = el.querySelector(".row-title");
      const percent = el.querySelector(".percent");
      const reset = el.querySelector(".reset");
      const fill = el.querySelector(".fill");
      if (!title || !percent || !reset || !fill) return;
      title.textContent = data.title != null ? data.title : labels()[key];
      percent.textContent = data.percentText || "--";
      reset.textContent = data.resetText || t("reset_placeholder");
      reset.dataset.warning = data.warning === true ? "true" : "false";
      percent.style.color = available ? color : "";
      window.PanelHooks.applyFillVisual(fill, { available, percent: data.percent, color });
    }

    function isCriticalRow(row) {
      return Boolean(row)
        && row.available === true
        && typeof row.percent === "number"
        && row.percent >= CRITICAL_PERCENT;
    }

    function applyCard(name, rows) {
      renderRow(name, "session", rows && rows.session);
      renderRow(name, "weekly", rows && rows.weekly);
      const cardEl = document.querySelector(`.card[data-card="${name}"]`);
      if (!cardEl) return;
      const critical = isCriticalRow(rows && rows.session)
        || isCriticalRow(rows && rows.weekly);
      cardEl.dataset.critical = critical ? "true" : "false";
    }

    function renderSourceProvenance(cardName, cardState) {
      const card = document.querySelector(`[data-card="${cardName}"]`);
      if (!card) return;
      const header = card.querySelector("header, .header, .brand") || card.firstElementChild;
      if (!header) return;
      let provenance = card.querySelector("[data-source-provenance]");
      if (!provenance) {
        provenance = document.createElement("span");
        provenance.className = "usage-source-provenance";
        provenance.dataset.sourceProvenance = "";
        const badge = document.createElement("span");
        badge.className = "usage-source-badge";
        badge.dataset.sourceBadge = "";
        const marker = document.createElement("span");
        marker.className = "usage-source-marker";
        marker.dataset.sourceMarker = "";
        provenance.append(badge, marker);
        header.append(provenance);
      }
      const data = cardState || {};
      const freshness = typeof data.freshness === "string" ? data.freshness : "unavailable";
      const badge = provenance.querySelector("[data-source-badge]");
      const marker = provenance.querySelector("[data-source-marker]");
      card.dataset.freshness = freshness;
      if (badge) badge.textContent = data.sourceLabel || "--";
      if (!marker) return;
      const age = data.ageText ? ` · ${data.ageText}` : "";
      if (freshness === "reauth_required") {
        marker.textContent = `${t("quota_reauth_required")}${age}`;
        marker.dataset.reauth = "true";
        marker.hidden = false;
      } else if (freshness === "stale") {
        marker.textContent = `${t("quota_stale")}${age}`;
        marker.dataset.reauth = "false";
        marker.hidden = false;
      } else {
        marker.textContent = "";
        marker.dataset.reauth = "false";
        marker.hidden = true;
      }
    }

    function setOpenRouterDetail(attribute, value) {
      const valueEl = document.querySelector(`[${attribute}]`);
      if (!valueEl) return;
      const row = valueEl.closest("[data-openrouter-detail]");
      const present = value !== null && value !== undefined && value !== "";
      if (row) row.hidden = !present;
      valueEl.textContent = present ? `${value}` : "--";
    }

    function renderOpenRouter(openrouter) {
      const data = openrouter || {};
      const card = document.querySelector('[data-card="openrouter"]');
      if (card) card.hidden = data.available !== true;
      const headline = document.querySelector("[data-openrouter-headline]");
      if (headline) {
        headline.textContent = data.headline === "unlimited"
          ? t("openrouter_unlimited")
          : data.headline && data.headline !== "unavailable" ? data.headline : "--";
      }
      setOpenRouterDetail("data-openrouter-account-remaining", data.accountRemaining);
      setOpenRouterDetail("data-openrouter-account-total", data.accountTotal);
      setOpenRouterDetail("data-openrouter-account-used", data.accountUsed);
      setOpenRouterDetail(
        "data-openrouter-key-limit",
        data.keyUnlimited === true ? t("openrouter_unlimited") : data.keyLimit
      );
      setOpenRouterDetail(
        "data-openrouter-key-remaining",
        data.keyUnlimited === true ? null : data.keyRemaining
      );
      setOpenRouterDetail(
        "data-openrouter-key-percent",
        typeof data.keyPercent === "number" ? `${data.keyPercent}%` : null
      );
      setOpenRouterDetail(
        "data-openrouter-reset-interval",
        data.resetInterval ? t(`openrouter_reset_interval_${data.resetInterval}`) : null
      );
      setOpenRouterDetail("data-openrouter-reset-at", data.resetText || data.resetAt);
    }

    function renderCodexStale(stale) {
      const staleEl = document.querySelector("[data-codex-stale]");
      const ageEl = document.querySelector("[data-codex-stale-age]");
      const tooltipEl = document.querySelector("[data-codex-stale-tooltip]");
      if (!staleEl || !ageEl || !tooltipEl) return;
      if (stale && stale.ageText) {
        ageEl.textContent = stale.ageText;
        tooltipEl.textContent = t("codex_stale_tooltip");
        staleEl.hidden = false;
        return;
      }
      ageEl.textContent = "";
      tooltipEl.textContent = "";
      staleEl.hidden = true;
    }

    function renderCodexCredits(credits) {
      const card = document.querySelector('[data-card="codex"]');
      if (!card) return;
      const existing = card.querySelector('[data-codex-credits]');
      if (!credits) {
        if (existing) existing.remove();
        return;
      }
      const el = existing || document.createElement("div");
      if (!existing) {
        el.className = "codex-credits";
        el.dataset.codexCredits = "";
        card.appendChild(el);
      }
      el.textContent = credits.unlimited
        ? t("codex_credits_unlimited")
        : t("codex_credits", { balance: credits.balance || "--" });
    }

    function renderAgy(agy) {
      applyCard("agy", agy);
      const staleEl = document.querySelector("[data-agy-stale]");
      const ageEl = document.querySelector("[data-agy-stale-age]");
      const tooltipEl = document.querySelector("[data-agy-stale-tooltip]");
      if (!staleEl || !ageEl || !tooltipEl) return;
      if (agy && agy.stale && agy.stale.ageText) {
        ageEl.textContent = agy.stale.ageText;
        tooltipEl.textContent = t("agy_stale_tooltip");
        staleEl.hidden = false;
        return;
      }
      ageEl.textContent = "";
      tooltipEl.textContent = "";
      staleEl.hidden = true;
    }

    function renderGrok(grok) {
      applyCard("grok", grok);
      const staleEl = document.querySelector("[data-grok-stale]");
      const ageEl = document.querySelector("[data-grok-stale-age]");
      const tooltipEl = document.querySelector("[data-grok-stale-tooltip]");
      if (!staleEl || !ageEl || !tooltipEl) return;
      if (grok && grok.stale && grok.stale.ageText) {
        ageEl.textContent = grok.stale.ageText;
        tooltipEl.textContent = t("grok_stale_tooltip");
        staleEl.hidden = false;
        return;
      }
      ageEl.textContent = "";
      tooltipEl.textContent = "";
      staleEl.hidden = true;
    }

    function renderHistoryLoadError(err) {
      const el = document.querySelector("[data-history-error]");
      const textEl = document.querySelector("[data-history-error-text]");
      const tooltipEl = document.querySelector("[data-history-error-tooltip]");
      if (!el || !textEl || !tooltipEl) return;
      if (err && err.reasonText) {
        textEl.textContent = err.reasonText;
        tooltipEl.textContent = t("history_load_error_tooltip");
        el.hidden = false;
        return;
      }
      textEl.textContent = "";
      tooltipEl.textContent = "";
      el.hidden = true;
    }

    // Every theme positions a stale/error tooltip with `right: 0` on its info icon so it
    // grows leftward from a fixed 14px glyph. That is fine near the right edge of the panel
    // but overflows past the window's left edge once the icon sits closer to the left (e.g.
    // the Codex row's "N ago" text pushes its icon left of where Claude's sits). Clamp it back
    // into the viewport on hover instead of re-tuning each theme's static CSS offset.
    function clampStaleTooltip(tooltipEl) {
      if (!tooltipEl) return;
      const margin = 8;
      // Re-measure from the CSS default (right: 0) each time — a stale inline offset from a
      // previous hover, or a window resize since then, would otherwise throw this off.
      tooltipEl.style.right = "";
      const rect = tooltipEl.getBoundingClientRect();
      if (rect.width === 0) return;
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
      const overflowLeft = margin - rect.left;
      if (overflowLeft <= 0) return;
      const maxShift = Math.max(0, viewportWidth - margin - rect.right);
      const shift = Math.min(overflowLeft, maxShift);
      if (shift > 0) tooltipEl.style.right = `${-shift}px`;
    }

    function bindStaleTooltipClamps() {
      const tooltips = document.querySelectorAll(
        "[data-codex-stale-tooltip], [data-agy-stale-tooltip], [data-grok-stale-tooltip], [data-history-error-tooltip]"
      );
      tooltips.forEach((tooltipEl) => {
        const infoEl = tooltipEl.parentElement;
        if (!infoEl) return;
        infoEl.addEventListener("mouseenter", () => clampStaleTooltip(tooltipEl));
      });
    }

    bindStaleTooltipClamps();

    function renderProjects(projects) {
      const list = document.querySelector('[data-project-list]');
      if (!list) return;
      const ranks = window.PanelHooks.projectRankLabels;
      const rows = (projects || []).slice(0, 3);
      const totalTokens = rows.reduce((s, p) => s + Math.max(0, Number(p.tokens) || 0), 0);
      list.replaceChildren();
      if (rows.length === 0) {
        const empty = document.createElement("div");
        empty.style.cssText = "color:var(--muted);font-size:13px;padding:8px 0";
        empty.textContent = t("projects_empty");
        list.append(empty);
        return;
      }
      rows.forEach((p, i) => {
        const tokens = Math.max(0, Number(p.tokens) || 0);
        const width = totalTokens > 0 ? Math.max(0, Math.min(100, (tokens / totalTokens) * 100)) : 0;
        const row = window.PanelHooks.renderProjectRow({ ...p, sharePercent: width }, i, ranks);
        if (row) list.append(row);
      });
    }

    function renderPeriodTotal(state) {
      const total = document.querySelector('[data-footer="today"]');
      if (!total) return;
      total.textContent = projectRange === "yesterday"
        ? (state.footer.yesterday || t("yesterday_text", { cost: "0.00", tokens: "0" }))
        : (state.footer.today || t("today_text", { cost: "0.00", tokens: "0" }));
    }





    function renderStatusline(statusline) {
      const data = statusline || {};
      const button = document.querySelector('[data-action="toggle-statusline"]');
      if (button) {
        const enabled = data.enabled === true;
        button.dataset.active = enabled ? "true" : "false";
        button.textContent = enabled ? t("cli_enabled") : t("cli_disabled");
      }
    }

    function relocateSwitchButton(state) {
      document.querySelectorAll(".usage-switch-host").forEach((host) => {
        host.classList.remove("usage-switch-host");
      });
      const openRouterUnavailable = !state.openrouter || state.openrouter.available !== true;
      const switchState = {
        ...state,
        hideOpenRouter: state.hideOpenRouter || openRouterUnavailable,
      };
      window.PanelHooks.switchButtonStrategy(switchState);
      const button = document.querySelector('[data-action="switch"]');
      if (button && state.hideClaude && state.hideCodex && state.hideAgy && state.hideGrok && !switchState.hideOpenRouter) {
        const openRouterHost = document.querySelector(
          '[data-card="openrouter"] .brand, [data-card="openrouter"] .header'
        );
        if (openRouterHost) {
          button.className = "switch";
          openRouterHost.appendChild(button);
        }
      }
      const host = button && button.parentElement;
      if (host && host.closest('[data-card="codex"]')) {
        host.classList.add("usage-switch-host");
      }
    }

    const QUOTA_CARD_IDS = ["claude", "codex", "agy", "grok", "openrouter"];

    function applyCardOrder(order) {
      if (cardDrag && cardDrag.dragging) return;
      if (!Array.isArray(order) || order.some((id) => typeof id !== "string" || !id) || new Set(order).size !== order.length) return;
      const knownOrder = order.filter((id) => QUOTA_CARD_IDS.includes(id));
      if (knownOrder.length !== QUOTA_CARD_IDS.length || !QUOTA_CARD_IDS.every((id) => knownOrder.includes(id))) return;
      const wrap = document.querySelector("main.wrap");
      const projects = document.querySelector('[data-card="projects"]');
      if (!wrap || !projects) return;
      const currentOrder = [...wrap.querySelectorAll(':scope > [data-card]')]
        .filter((card) => QUOTA_CARD_IDS.includes(card.dataset.card))
        .map((card) => card.dataset.card);
      if (knownOrder.every((id, index) => currentOrder[index] === id)) return;
      knownOrder.forEach((id) => {
        const card = document.querySelector(`[data-card="${id}"]`);
        if (card) wrap.insertBefore(card, projects);
      });
    }

    window.usageApplyState = function usageApplyState(state) {
      if (typeof state.system_accent_color === "string" && state.system_accent_color) {
        root.style.setProperty("--usage-system-accent", state.system_accent_color);
      } else {
        root.style.removeProperty("--usage-system-accent");
      }
      document.documentElement.classList.toggle('hide-codex', !!state.hideCodex);
      document.documentElement.classList.toggle('hide-claude', !!state.hideClaude);
      document.documentElement.classList.toggle('hide-agy', !!state.hideAgy);
      document.documentElement.classList.toggle('hide-grok', !!state.hideGrok);
      document.documentElement.classList.toggle('hide-openrouter', !!state.hideOpenRouter);
      applyCardOrder(state.cardOrder);
      relocateSwitchButton(state);
      currentLanguage = I18N[state.language] ? state.language : currentLanguage;
      applyStaticText();
      applyCard("claude", state.claude);
      applyCard("codex", state.codex);
      renderCodexStale(state.codex && state.codex.stale);
      renderCodexCredits(state.codex && state.codex.credits);
      renderAgy(state.agy);
      renderGrok(state.grok);
      renderOpenRouter(state.openrouter);
      renderSourceProvenance("claude", state.claude);
      renderSourceProvenance("codex", state.codex);
      renderSourceProvenance("agy", state.agy);
      renderSourceProvenance("grok", state.grok);
      renderSourceProvenance("openrouter", state.openrouter);
      renderHistoryLoadError(state.historyError);
      latestState = state;
      renderProjects(
        projectRange === "1d" ? state.projects
        : projectRange === "yesterday" ? state.projectsYesterday
        : projectRange === "7d" ? state.projects7d
        : projectRange === "30d" ? state.projects30d
        : projectRange === "all" ? state.projectsAll
        : state.projects
      );
      renderStatusline(state.statusline || {});
      const rate = document.querySelector('[data-footer="rate"]');
      const status = document.querySelector('[data-footer="status"]');
      const serviceAlerts = document.querySelector('[data-footer="service-alerts"]');
      const install = document.querySelector('[data-action="install"]');
      if (rate) rate.textContent = state.footer.rate || t("rate_text", { value: "--" });
      if (status) status.textContent = state.footer.status || t("status_text", { value: "--" });
      renderPeriodTotal(state);
      if (install) install.dataset.visible = state.footer.showInstall === true ? "true" : "false";
      if (serviceAlerts) {
        const alerts = state.footer.serviceAlerts || [];
        serviceAlerts.replaceChildren(...alerts.slice(0, 2).map((alert) => {
          const item = document.createElement("div");
          item.className = "service-alert";
          item.textContent = alert;
          return item;
        }));
        serviceAlerts.hidden = alerts.length === 0;
      }
    };

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-action]");
      if (!button) return;
      if (button.dataset.action === "toggle-project-range") {
        projectRange = projectRange === "1d" ? "yesterday" : projectRange === "yesterday" ? "7d" : projectRange === "7d" ? "30d" : projectRange === "30d" ? "all" : "1d";
        button.textContent = projectRangeLabel(projectRange);
        if (latestState) {
          renderProjects(
            projectRange === "1d" ? latestState.projects
            : projectRange === "yesterday" ? latestState.projectsYesterday
            : projectRange === "7d" ? latestState.projects7d
            : projectRange === "30d" ? latestState.projects30d
            : projectRange === "all" ? latestState.projectsAll
            : latestState.projects
          );
          renderPeriodTotal(latestState);
          if (typeof window.usageRequestContentHeight === "function") {
            window.usageRequestContentHeight();
          }
        }
        return;
      }
      const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.usage;
      if (bridge && typeof bridge.postMessage === "function") {
        bridge.postMessage(button.dataset.action);
      }
    });

    let cardDrag = null;

    document.addEventListener("pointerdown", (event) => {
      const card = event.target.closest('[data-card="claude"], [data-card="codex"], [data-card="agy"], [data-card="grok"], [data-card="openrouter"]');
      if (!card || event.button !== 0 || event.target.closest(window.PanelHooks.pointerdownExcludeSelector)) return;
      cardDrag = { card, pointerId: event.pointerId, startY: event.clientY, dragging: false };
      card.setPointerCapture(event.pointerId);
    });

    document.addEventListener("pointermove", (event) => {
      if (!cardDrag || event.pointerId !== cardDrag.pointerId) return;
      if (!cardDrag.dragging) {
        if (Math.abs(event.clientY - cardDrag.startY) <= 4) return;
        cardDrag.dragging = true;
        cardDrag.card.classList.add("is-dragging");
        document.documentElement.classList.add("is-card-dragging");
      }
      event.preventDefault();
      const cards = [...document.querySelectorAll('[data-card="claude"], [data-card="codex"], [data-card="agy"], [data-card="grok"], [data-card="openrouter"]')]
        .filter((card) => card.offsetParent !== null);
      const target = cards.find((card) => card !== cardDrag.card && event.clientY < card.getBoundingClientRect().top + card.getBoundingClientRect().height / 2);
      if (target) target.parentElement.insertBefore(cardDrag.card, target);
      else {
        const projects = document.querySelector('[data-card="projects"]');
        if (projects) projects.parentElement.insertBefore(cardDrag.card, projects);
      }
    });

    function finishCardDrag(event) {
      if (!cardDrag || event.pointerId !== cardDrag.pointerId) return;
      const { card, dragging } = cardDrag;
      if (card.hasPointerCapture(event.pointerId)) card.releasePointerCapture(event.pointerId);
      card.classList.remove("is-dragging");
      document.documentElement.classList.remove("is-card-dragging");
      cardDrag = null;
      if (!dragging) return;
      const order = [...document.querySelectorAll('main.wrap > [data-card]')]
        .filter((item) => QUOTA_CARD_IDS.includes(item.dataset.card))
        .map((item) => item.dataset.card);
      const bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.usage;
      if (bridge && typeof bridge.postMessage === "function") {
        bridge.postMessage(JSON.stringify({ action: "set_card_order", order }));
      }
    }

    document.addEventListener("pointerup", finishCardDrag);
    document.addEventListener("pointercancel", finishCardDrag);

    window.usageApplyState({
      language: "en",
      claude: { session: {}, weekly: {} },
      codex: { session: {}, weekly: {} },
      agy: { session: {}, weekly: {}, groupName: "" },
      grok: { weekly: {} },
      openrouter: {},
      cardOrder: ["claude", "codex", "agy", "grok", "openrouter"],
      hideAgy: true,
      hideGrok: true,
      hideOpenRouter: false,
      projects: [],
      projectsYesterday: [],
      projects7d: [],
      projects30d: [],
      projectsAll: [],
      statusline: {},
      footer: { rate: "Rate: --", status: "Status: Loading", today: "Today: $0.00 (0 tokens)", yesterday: "Yesterday: $0.00 (0 tokens)", serviceAlerts: [], showInstall: false }
    });
