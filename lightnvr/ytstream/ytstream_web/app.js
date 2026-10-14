// Global state
let currentStreams = [];
let youtubeAccounts = [];

async function openAccountsModal() {
    try {
        const response = await fetch('api/accounts');
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Could not load accounts');
        youtubeAccounts = data.accounts;
        const list = document.getElementById('accounts-list');
        list.replaceChildren();
        for (const account of youtubeAccounts) {
            const row = document.createElement('div');
            row.className = 'form-group';
            const text = document.createElement('span');
            text.textContent = `${account.label} (${account.id}) — ${account.authenticated ? 'Token saved' : 'Authorization required'}${account.retry ? ' — ' + account.retry.message : ''} `;
            const login = document.createElement('a');
            login.className = 'btn btn-secondary';
            login.textContent = 'Authorize';
            login.href = 'login?account_id=' + encodeURIComponent(account.id);
            row.append(text, login);
            if (account.retry?.manual_retry) {
                const retry = document.createElement('button');
                retry.className = 'btn btn-secondary';
                retry.textContent = 'Retry';
                retry.onclick = async () => {
                    const response = await fetch('api/youtube/retry?account_id=' + encodeURIComponent(account.id), {method: 'POST'});
                    const data = await response.json();
                    if (!response.ok) alert(data.error);
                    await openAccountsModal();
                };
                row.appendChild(retry);
            }
            list.appendChild(row);
        }
        document.getElementById('accounts-modal').classList.remove('hide');
    } catch (error) {
        alert(error.message);
    }
}

async function addYouTubeAccount() {
    try {
        const id = document.getElementById('new-account-id').value.trim();
        const label = document.getElementById('new-account-label').value.trim() || id;
        const response = await fetch('api/accounts', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, label})
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Could not add account');
        document.getElementById('new-account-id').value = '';
        document.getElementById('new-account-label').value = '';
        await openAccountsModal();
    } catch (error) {
        alert(error.message);
    }
}
let currentSettings = { history_days_limit: 30 };
let selectedGlobalDate = "";
let cardSpecificDates = {};
let activeCameraForModal = null;
let modalCameraHistoryDays = [];

// Initialize on page load
document.addEventListener("DOMContentLoaded", () => {
    loadSettings();
    refreshStatus();
    // Refresh status every 5 seconds
    setInterval(refreshStatus, 5000);
});

// Load Settings from Server
async function loadSettings() {
    try {
        const response = await fetch("api/settings");
        if (response.ok) {
            const data = await response.json();
            currentSettings = data;
            updateSettingsUI(data);
        }
    } catch (error) {
        console.error("Error loading settings:", error);
    }
}

// Update Settings UI elements
function updateSettingsUI(settings) {
    const days = settings.history_days_limit || 30;
    const pillText = document.getElementById("history-pill-text");
    if (pillText) pillText.innerText = `Past ${days} Days`;

    const badgeLimit = document.getElementById("badge-current-limit");
    if (badgeLimit) badgeLimit.innerText = `${days} Days`;

    const inputDays = document.getElementById("setting-history-days-input");
    if (inputDays) inputDays.value = days;

    // Update active preset button
    document.querySelectorAll(".btn-preset").forEach(btn => {
        btn.classList.toggle("active", parseInt(btn.dataset.days) === days);
    });
}

// Refresh statuses, specs and auth status
async function refreshStatus() {
    try {
        const response = await fetch("api/status");
        if (!response.ok) throw new Error("API status request failed");
        const data = await response.json();
        youtubeAccounts = data.accounts || [];
        
        // Update System Specs
        document.getElementById("cpu-usage").innerText = `${data.system.cpu}%`;
        document.getElementById("ram-usage").innerText = `${data.system.ram}%`;
        
        // Update Auth Status Badge
        const authDot = document.getElementById("auth-dot");
        const authText = document.getElementById("auth-text");
        const authBtn = document.getElementById("auth-btn");
        
        if (data.authenticated) {
            authDot.className = "status-dot active";
            authText.innerText = "Authorized";
            authBtn.classList.add("hide");
        } else {
            authDot.className = "status-dot inactive";
            authText.innerText = "OAuth Credentials Missing / Expired";
            authBtn.classList.remove("hide");
        }

        const youtube = data.youtube || {};
        const notice = document.getElementById("youtube-notice");
        notice.classList.toggle("hide", !youtube.blocked);
        document.getElementById("youtube-notice-text").textContent = youtube.blocked
            ? youtube.message + (youtube.retry_at ? ` Retry after ${new Date(youtube.retry_at).toLocaleString()}.` : "")
            : "";
        document.getElementById("youtube-retry-btn").classList.toggle("hide", !youtube.manual_retry);
        
        // Update Settings if returned
        if (data.settings) {
            currentSettings = data.settings;
            updateSettingsUI(data.settings);
        }

        // Render Stream Cards
        currentStreams = data.streams;
        renderStreamCards(data.streams);
        updateLogSelect(data.streams);
        
        // Initialize/Render Lucide Icons
        lucide.createIcons();
    } catch (error) {
        console.error("Error refreshing status:", error);
    }
}

// Render the grid of streams with previous days & specific date URLs
function renderStreamCards(streams) {
    const grid = document.getElementById("streams-grid");
    grid.innerHTML = "";
    
    if (streams.length === 0) {
        grid.innerHTML = `
            <div class="loading-state">
                <i data-lucide="video-off" style="width: 48px; height: 48px;"></i>
                <p>No streams configured. Click "Configure Streams" to add one.</p>
            </div>
        `;
        lucide.createIcons();
        return;
    }
    
    streams.forEach(s => {
        const card = document.createElement("div");
        card.className = "stream-card";
        
        const isActive = s.status === "Active";
        const isPaused = !isActive && !!s.retry;
        const statusText = isPaused ? (s.retry.retry_at ? "Waiting" : "Paused") : s.status;
        const statusBadgeClass = isActive ? "stream-badge active" : "stream-badge inactive";
        const controlBtnText = isActive ? "Stop" : "Start";
        const controlBtnClass = isActive ? "btn btn-danger" : "btn btn-primary";
        const controlBtnIcon = isActive ? "square" : "play";
        const controlBtnClick = isActive ? `stopStream('${s.stream_name}')` : `startStream('${s.stream_name}')`;
        
        const watchUrlHtml = s.watch_url 
            ? `<div class="url-copy-row">
                   <a href="${s.watch_url}" target="_blank" class="watch-link">
                       <i data-lucide="youtube"></i> ${s.watch_url}
                   </a>
                   <button class="btn-copy-sm" onclick="copyToClipboard('${s.watch_url}', event)" title="Copy Watch URL">
                       <i data-lucide="copy"></i>
                   </button>
               </div>`
            : `<span style="color: var(--color-text-dim);">No URL (Starts when live)</span>`;
            
        // Process Previous Days History
        const allPrevDays = s.previous_days || [];
        const cardSpecificDate = cardSpecificDates[s.stream_name] || selectedGlobalDate;
        
        let displayedDays = allPrevDays;
        let isFilteredByDate = false;
        
        if (cardSpecificDate) {
            displayedDays = allPrevDays.filter(d => d.date === cardSpecificDate);
            isFilteredByDate = true;
        }

        // Render previous days list HTML
        let historySectionHtml = "";
        if (displayedDays.length > 0) {
            const daysCardsHtml = displayedDays.map(dayItem => {
                const sessionButtonsHtml = dayItem.sessions.map(sess => {
                    const isMorning = sess.session === "_1";
                    const sessionBadgeClass = isMorning ? "session-pill morning" : "session-pill afternoon";
                    const sessionIcon = isMorning ? "sun" : "moon";
                    const shortLabel = isMorning ? "Part 1 (00h-12h)" : "Part 2 (12h-24h)";
                    
                    return `
                        <div class="session-item">
                            <span class="${sessionBadgeClass}">
                                <i data-lucide="${sessionIcon}"></i> ${shortLabel}
                            </span>
                            <a href="${sess.watch_url}" target="_blank" class="session-url-btn" title="Watch on YouTube: ${sess.title}">
                                <i data-lucide="external-link"></i> Watch
                            </a>
                            <button class="btn-copy-mini" onclick="copyToClipboard('${sess.watch_url}', event)" title="Copy YouTube Link">
                                <i data-lucide="copy"></i>
                            </button>
                        </div>
                    `;
                }).join("");

                return `
                    <div class="history-day-card">
                        <div class="day-card-header">
                            <div class="day-date">
                                <i data-lucide="calendar"></i>
                                <span>${formatDateLabel(dayItem.date)}</span>
                            </div>
                            <span class="day-session-count">${dayItem.sessions.length} session${dayItem.sessions.length > 1 ? 's' : ''}</span>
                        </div>
                        <div class="day-sessions-list">
                            ${sessionButtonsHtml}
                        </div>
                    </div>
                `;
            }).join("");

            historySectionHtml = `
                <div class="history-days-scroll">
                    ${daysCardsHtml}
                </div>
            `;
        } else {
            if (isFilteredByDate) {
                historySectionHtml = `
                    <div class="history-empty-box">
                        <i data-lucide="calendar-x"></i>
                        <p>No stream recording found for <strong>${cardSpecificDate}</strong>.</p>
                        <button class="btn btn-sm btn-secondary margin-top-xs" onclick="clearCardSpecificDay('${s.stream_name}')">
                            Show All Days
                        </button>
                    </div>
                `;
            } else {
                historySectionHtml = `
                    <div class="history-empty-box">
                        <i data-lucide="film"></i>
                        <p>No previous stream URLs recorded in the last ${currentSettings.history_days_limit} days.</p>
                    </div>
                `;
            }
        }

        const filterNoticeHtml = isFilteredByDate ? `
            <div class="filter-active-pill">
                <span>Filtered for <strong>${cardSpecificDate}</strong></span>
                <button onclick="clearCardSpecificDay('${s.stream_name}')" title="Clear filter"><i data-lucide="x"></i></button>
            </div>
        ` : '';

        card.innerHTML = `
            <div class="stream-card-header">
                <div class="stream-info">
                    <h3>${s.stream_name}</h3>
                </div>
                <span class="${statusBadgeClass}">${statusText}</span>
            </div>
            
            <div class="stream-details">
                <div class="detail-row">
                    <span class="detail-label">Source RTSP URL</span>
                    <span class="detail-value" title="${s.url}">${s.url}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">Today's Live YouTube URL</span>
                    <div class="detail-value">${watchUrlHtml}</div>
                </div>
            </div>

            <!-- Previous Days URLs Section -->
            <div class="previous-days-section">
                <div class="history-section-header">
                    <div class="section-title-wrapper">
                        <i data-lucide="history" class="text-primary"></i>
                        <h4>Previous Days URLs</h4>
                        <span class="history-limit-tag">${isFilteredByDate ? '1 day' : `${allPrevDays.length} days`}</span>
                    </div>
                    <button class="btn btn-sm btn-secondary" onclick="openCameraHistoryModal('${s.stream_name}', '${cardSpecificDate || ''}')" title="Open full history timeline & preview player">
                        <i data-lucide="external-link"></i> Full History
                    </button>
                </div>

                <!-- Specific Day Picker for this Camera -->
                <div class="card-date-filter-row">
                    <label for="card-date-${s.stream_name}">
                        <i data-lucide="calendar-search"></i> Specific Day:
                    </label>
                    <input type="date" id="card-date-${s.stream_name}" class="input-card-date" value="${cardSpecificDate || ''}" onchange="filterCardSpecificDay('${s.stream_name}', this.value)" title="Choose a specific day to show URLs">
                    ${isFilteredByDate ? `<button class="btn-clear-date-mini" onclick="clearCardSpecificDay('${s.stream_name}')" title="Clear day filter"><i data-lucide="x"></i></button>` : ''}
                </div>

                ${filterNoticeHtml}
                ${historySectionHtml}
            </div>
            
            <div class="stream-controls">
                <button class="${controlBtnClass}" onclick="${controlBtnClick}" ${isPaused ? 'disabled' : ''}>
                    <i data-lucide="${controlBtnIcon}"></i> ${controlBtnText}
                </button>
                <button class="btn btn-secondary" onclick="selectAndLoadLogs('${s.stream_name}')">
                    <i data-lucide="terminal"></i> View Logs
                </button>
            </div>
        `;
        if (isPaused) {
            const message = document.createElement("p");
            message.className = "stream-retry-message";
            message.textContent = s.retry.message + (s.retry.retry_at
                ? ` Retry after ${new Date(s.retry.retry_at).toLocaleString()}.` : "");
            card.querySelector(".stream-details").appendChild(message);
        }
        grid.appendChild(card);
    });
}

// Format Date label nicely e.g. "2026-09-06 (Yesterday)"
function formatDateLabel(dateStr) {
    if (!dateStr) return "";
    try {
        const parts = dateStr.split("-");
        const target = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        target.setHours(0, 0, 0, 0);
        
        const diffDays = Math.round((today - target) / (1000 * 60 * 60 * 24));
        if (diffDays === 0) return `${dateStr} (Today)`;
        if (diffDays === 1) return `${dateStr} (Yesterday)`;
        if (diffDays > 1 && diffDays <= 7) return `${dateStr} (${diffDays} days ago)`;
        return dateStr;
    } catch (e) {
        return dateStr;
    }
}

// Global Date Filter Handler
function onGlobalDateChange(dateVal) {
    selectedGlobalDate = dateVal;
    const clearBtn = document.getElementById("clear-date-btn");
    if (clearBtn) {
        if (dateVal) clearBtn.classList.remove("hide");
        else clearBtn.classList.add("hide");
    }
    renderStreamCards(currentStreams);
    lucide.createIcons();
}

function clearGlobalDateFilter() {
    selectedGlobalDate = "";
    const picker = document.getElementById("global-date-picker");
    if (picker) picker.value = "";
    const clearBtn = document.getElementById("clear-date-btn");
    if (clearBtn) clearBtn.classList.add("hide");
    renderStreamCards(currentStreams);
    lucide.createIcons();
}

// Per-card Specific Day Filter Handlers
function filterCardSpecificDay(streamName, dateVal) {
    if (dateVal) {
        cardSpecificDates[streamName] = dateVal;
    } else {
        delete cardSpecificDates[streamName];
    }
    renderStreamCards(currentStreams);
    lucide.createIcons();
}

function clearCardSpecificDay(streamName) {
    delete cardSpecificDates[streamName];
    renderStreamCards(currentStreams);
    lucide.createIcons();
}

// Settings Modal Handlers
function openSettingsModal() {
    updateSettingsUI(currentSettings);
    document.getElementById("settings-modal").classList.remove("hide");
    lucide.createIcons();
}

function closeSettingsModal() {
    document.getElementById("settings-modal").classList.add("hide");
}

function setPresetDays(days) {
    const input = document.getElementById("setting-history-days-input");
    if (input) input.value = days;
    
    document.querySelectorAll(".btn-preset").forEach(btn => {
        btn.classList.toggle("active", parseInt(btn.dataset.days) === days);
    });

    const badgeLimit = document.getElementById("badge-current-limit");
    if (badgeLimit) badgeLimit.innerText = `${days} Days`;
}

function onCustomDaysInput(val) {
    const days = parseInt(val) || 30;
    document.querySelectorAll(".btn-preset").forEach(btn => {
        btn.classList.toggle("active", parseInt(btn.dataset.days) === days);
    });
    const badgeLimit = document.getElementById("badge-current-limit");
    if (badgeLimit) badgeLimit.innerText = `${days} Days`;
}

async function saveSettingsConfig() {
    const input = document.getElementById("setting-history-days-input");
    const days = Math.max(1, Math.min(365, parseInt(input.value) || 30));
    
    try {
        const response = await fetch("api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ history_days_limit: days })
        });
        
        const data = await response.json();
        if (!response.ok) throw new Error(data.message || "Failed to save settings");
        
        currentSettings = data.settings;
        updateSettingsUI(currentSettings);
        closeSettingsModal();
        showToast(`Settings saved: Showing last ${days} days.`);
        
        // Dynamically update view immediately with new day limit
        refreshStatus();
    } catch (e) {
        alert(`Error saving settings: ${e.message}`);
    }
}

// YouTube Archive Sync
async function triggerHistorySync() {
    const btn = document.getElementById("btn-sync-history");
    const feedback = document.getElementById("sync-feedback");
    btn.disabled = true;
    btn.innerHTML = `<div class="spinner-sm"></div> Syncing...`;
    feedback.classList.remove("hide");
    feedback.innerText = "Scanning YouTube Data API for broadcasts...";
    feedback.className = "sync-feedback-text";
    
    try {
        const response = await fetch("api/history/sync", { method: "POST" });
        const data = await response.json();
        feedback.innerText = data.message;
        feedback.className = "sync-feedback-text success";
        showToast(data.message);
        refreshStatus();
    } catch (e) {
        feedback.innerText = `Sync failed: ${e.message}`;
        feedback.className = "sync-feedback-text error";
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<i data-lucide="refresh-cw"></i> Sync History from YouTube`;
        lucide.createIcons();
    }
}

// Camera History Modal Handlers
async function openCameraHistoryModal(streamName, initialDate = "") {
    activeCameraForModal = streamName;
    document.getElementById("history-modal-cam-name").innerText = `${streamName} - Recording History`;
    document.getElementById("history-modal-cam-sub").innerText = `Previous stream archives (Limit: ${currentSettings.history_days_limit} days)`;
    
    const datePicker = document.getElementById("history-modal-date-picker");
    if (datePicker) datePicker.value = initialDate || "";
    
    closeVideoPreview();
    document.getElementById("camera-history-modal").classList.remove("hide");
    lucide.createIcons();
    
    await loadCameraHistoryData(streamName, initialDate);
}

function closeCameraHistoryModal() {
    document.getElementById("camera-history-modal").classList.add("hide");
    closeVideoPreview();
}

async function loadCameraHistoryData(streamName, specificDate = "") {
    const container = document.getElementById("history-timeline-container");
    container.innerHTML = `
        <div class="loading-state">
            <div class="spinner"></div>
            <p>Loading historical stream recordings...</p>
        </div>
    `;
    
    try {
        let url = `api/history?stream_name=${encodeURIComponent(streamName)}&days=${currentSettings.history_days_limit}`;
        if (specificDate) {
            url += `&date=${encodeURIComponent(specificDate)}`;
        }
        
        const response = await fetch(url);
        if (!response.ok) throw new Error("Failed to fetch camera history");
        const data = await response.json();
        
        const camData = data.history[streamName] || { days: [], total_records: 0 };
        modalCameraHistoryDays = camData.days || [];
        
        // Update stats badge
        const statsEl = document.getElementById("history-modal-stats");
        const clearDateBtn = document.getElementById("history-modal-clear-date");
        if (specificDate) {
            statsEl.innerText = `Filtered for ${specificDate} (${modalCameraHistoryDays.length} day found)`;
            if (clearDateBtn) clearDateBtn.classList.remove("hide");
        } else {
            statsEl.innerText = `Showing last ${currentSettings.history_days_limit} days (${camData.total_records} sessions across ${modalCameraHistoryDays.length} days)`;
            if (clearDateBtn) clearDateBtn.classList.add("hide");
        }
        
        renderCameraHistoryTimeline(modalCameraHistoryDays, specificDate);
    } catch (e) {
        container.innerHTML = `
            <div class="history-empty-box">
                <i data-lucide="alert-circle" style="color: var(--color-danger);"></i>
                <p>Failed to load stream history: ${e.message}</p>
            </div>
        `;
        lucide.createIcons();
    }
}

function filterModalHistoryByDate(dateVal) {
    if (!activeCameraForModal) return;
    loadCameraHistoryData(activeCameraForModal, dateVal);
}

function clearModalDateFilter() {
    const picker = document.getElementById("history-modal-date-picker");
    if (picker) picker.value = "";
    if (activeCameraForModal) {
        loadCameraHistoryData(activeCameraForModal, "");
    }
}

function renderCameraHistoryTimeline(daysList, specificDate = "") {
    const container = document.getElementById("history-timeline-container");
    container.innerHTML = "";
    
    if (daysList.length === 0) {
        container.innerHTML = `
            <div class="history-empty-box">
                <i data-lucide="calendar-x"></i>
                <p>${specificDate ? `No stream recording found for date <strong>${specificDate}</strong>.` : `No recordings found in the last ${currentSettings.history_days_limit} days.`}</p>
            </div>
        `;
        lucide.createIcons();
        return;
    }
    
    daysList.forEach(dayItem => {
        const itemEl = document.createElement("div");
        itemEl.className = "timeline-day-card";
        
        const sessionsHtml = dayItem.sessions.map(sess => {
            const isMorning = sess.session === "_1";
            const sessionIcon = isMorning ? "sun" : "moon";
            const sessionTagClass = isMorning ? "session-pill morning" : "session-pill afternoon";
            
            return `
                <div class="timeline-session-row">
                    <div class="session-info">
                        <span class="${sessionTagClass}">
                            <i data-lucide="${sessionIcon}"></i> ${sess.session_label}
                        </span>
                        <span class="session-title">${sess.title || 'Live Broadcast'}</span>
                        <span class="session-status-tag ${sess.status}">${sess.status}</span>
                    </div>
                    <div class="session-actions">
                        <button class="btn btn-sm btn-secondary" onclick="openVideoPreview('${sess.broadcast_id}', '${sess.title}')" title="Preview YouTube stream">
                            <i data-lucide="play-circle"></i> Watch Preview
                        </button>
                        <a href="${sess.watch_url}" target="_blank" class="btn btn-sm btn-primary" title="Open in YouTube">
                            <i data-lucide="youtube"></i> Open YouTube
                        </a>
                        <button class="btn btn-sm btn-secondary" onclick="copyToClipboard('${sess.watch_url}', event)" title="Copy Watch URL">
                            <i data-lucide="copy"></i> Copy Link
                        </button>
                    </div>
                </div>
            `;
        }).join("");

        itemEl.innerHTML = `
            <div class="timeline-day-header">
                <div class="day-badge-wrap">
                    <i data-lucide="calendar"></i>
                    <span class="day-title">${formatDateLabel(dayItem.date)}</span>
                </div>
                <span class="day-subtitle">${dayItem.date}</span>
            </div>
            <div class="timeline-sessions-wrap">
                ${sessionsHtml}
            </div>
        `;
        container.appendChild(itemEl);
    });
    
    lucide.createIcons();
}

// Video Preview Player in History Modal
function openVideoPreview(broadcastId, title) {
    if (!broadcastId) return;
    const previewContainer = document.getElementById("history-preview-container");
    const previewIframe = document.getElementById("history-preview-iframe");
    const previewTitle = document.getElementById("preview-title");
    
    previewTitle.innerText = `Preview: ${title || 'YouTube Stream'}`;
    previewIframe.src = `https://www.youtube.com/embed/${broadcastId}?autoplay=1`;
    previewContainer.classList.remove("hide");
    previewContainer.scrollIntoView({ behavior: "smooth" });
}

function closeVideoPreview() {
    const previewContainer = document.getElementById("history-preview-container");
    const previewIframe = document.getElementById("history-preview-iframe");
    if (previewIframe) previewIframe.src = "";
    if (previewContainer) previewContainer.classList.add("hide");
}

// Copy to Clipboard Utility
function copyToClipboard(text, event) {
    if (event) event.stopPropagation();
    if (!text) return;
    
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
            showToast("YouTube Watch URL copied to clipboard!");
        }).catch(() => {
            fallbackCopy(text);
        });
    } else {
        fallbackCopy(text);
    }
}

function fallbackCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
        document.execCommand("copy");
        showToast("YouTube Watch URL copied to clipboard!");
    } catch (e) {
        prompt("Copy URL:", text);
    }
    document.body.removeChild(ta);
}

// Toast Feedback Notification
function showToast(message) {
    const toast = document.getElementById("toast");
    const msgEl = document.getElementById("toast-message");
    if (!toast || !msgEl) return;
    
    msgEl.innerText = message;
    toast.classList.remove("hide");
    toast.classList.add("show");
    
    setTimeout(() => {
        toast.classList.remove("show");
        toast.classList.add("hide");
    }, 3200);
}

// Update the logs dropdown selection
function updateLogSelect(streams) {
    const select = document.getElementById("log-stream-select");
    const currentVal = select.value;
    
    select.innerHTML = '<option value="">Select Stream...</option>';
    
    streams.forEach(s => {
        const opt = document.createElement("option");
        opt.value = s.stream_name;
        opt.innerText = s.stream_name;
        select.appendChild(opt);
    });
    
    if (streams.some(s => s.stream_name === currentVal)) {
        select.value = currentVal;
    }
}

// Select stream and load logs immediately
function selectAndLoadLogs(streamName) {
    const select = document.getElementById("log-stream-select");
    select.value = streamName;
    loadLogs();
    document.querySelector(".logs-section").scrollIntoView({ behavior: "smooth" });
}

// Load logs for the selected stream
async function loadLogs() {
    const select = document.getElementById("log-stream-select");
    const output = document.getElementById("log-output");
    const streamName = select.value;
    
    if (!streamName) {
        output.innerText = "Select a camera above to display live logs.";
        return;
    }
    
    output.innerText = "Fetching logs...";
    
    try {
        const response = await fetch(`api/logs/${encodeURIComponent(streamName)}`);
        if (!response.ok) throw new Error("Failed to fetch logs");
        const data = await response.json();
        
        if (data.logs.length === 0) {
            output.innerText = "Log file is empty.";
        } else {
            output.innerText = data.logs.join("");
            const terminal = document.querySelector(".terminal-body");
            terminal.scrollTop = terminal.scrollHeight;
        }
    } catch (error) {
        output.innerText = `Error: ${error.message}`;
    }
}

// Start Stream Action
async function retryYouTube() {
    const button = document.getElementById("youtube-retry-btn");
    button.disabled = true;
    try {
        const response = await fetch("api/youtube/retry", { method: "POST" });
        const data = await response.json();
        showToast(data.message || data.error);
        await refreshStatus();
    } catch (error) {
        showToast(`Could not retry YouTube: ${error.message}`);
    } finally {
        button.disabled = false;
    }
}

async function startStream(streamName) {
    try {
        const response = await fetch("api/stream/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ stream_name: streamName })
        });
        const data = await response.json();
        showToast(data.message || data.error);
        refreshStatus();
    } catch (e) {
        alert(`Failed to start stream: ${e.message}`);
    }
}

// Stop Stream Action
async function stopStream(streamName) {
    try {
        const response = await fetch("api/stream/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ stream_name: streamName })
        });
        const data = await response.json();
        showToast(data.message || data.error);
        refreshStatus();
    } catch (e) {
        alert(`Failed to stop stream: ${e.message}`);
    }
}

// Trigger Cleanup API
async function triggerCleanup() {
    if (!confirm("Are you sure you want to clean up all old stream configurations older than today? This might consume significant API quota!")) {
        return;
    }
    try {
        const response = await fetch("api/cleanup", { method: "POST" });
        const data = await response.json();
        showToast(data.message);
    } catch (e) {
        alert(`Failed to trigger cleanup: ${e.message}`);
    }
}

// Modal Configuration Helpers
function openStreamsModal() {
    const list = document.getElementById("modal-streams-list");
    list.innerHTML = "";
    
    currentStreams.forEach(s => {
        createStreamRowElement(s.stream_name, s.url, s.protocol || "tcp", s.account_id || "default", s.fallback_account_ids || []);
    });
    
    if (currentStreams.length === 0) {
        addNewStreamRow();
    }
    
    document.getElementById("streams-modal").classList.remove("hide");
    lucide.createIcons();
}

function closeStreamsModal() {
    document.getElementById("streams-modal").classList.add("hide");
}

function createStreamRowElement(name = "", url = "", protocol = "tcp", accountId = "default", fallbacks = []) {
    const list = document.getElementById("modal-streams-list");
    const row = document.createElement("div");
    row.className = "stream-row";
    row.innerHTML = `
        <div class="stream-inputs">
            <div class="form-group">
                <label>Stream Name</label>
                <input type="text" class="input-name" value="${name}" placeholder="e.g. front-door">
            </div>
            <div class="form-group">
                <label>RTSP Source URL</label>
                <input type="text" class="input-url" value="${url}" placeholder="rtsp://host:port/path">
            </div>
            <div class="form-group">
                <label>Transport Protocol</label>
                <input type="text" class="input-protocol" value="${protocol}" placeholder="tcp or udp">
            </div>
        </div>
        <button class="btn btn-close" onclick="deleteStreamRow(this)" title="Delete Row">
            <i data-lucide="trash"></i>
        </button>
    `;
    const inputs = row.querySelector('.stream-inputs');
    for (const [labelText, className, multiple] of [['YouTube Account', 'input-account', false], ['OAuth fallback accounts (optional)', 'input-fallbacks', true]]) {
        const group = document.createElement('div');
        group.className = 'form-group';
        const label = document.createElement('label');
        label.textContent = labelText;
        const select = document.createElement('select');
        select.className = className;
        select.multiple = multiple;
        for (const account of youtubeAccounts) {
            const option = new Option(account.label, account.id);
            option.selected = multiple ? fallbacks.includes(account.id) : account.id === accountId;
            select.add(option);
        }
        group.append(label, select);
        inputs.appendChild(group);
    }
    list.appendChild(row);
}

function addNewStreamRow() {
    createStreamRowElement();
    lucide.createIcons();
    const modalBody = document.querySelector(".modal-body");
    modalBody.scrollTop = modalBody.scrollHeight;
}

function deleteStreamRow(btn) {
    const row = btn.closest(".stream-row");
    row.remove();
}

// Save streams configuration
async function saveStreamsConfig() {
    const list = document.getElementById("modal-streams-list");
    const rows = list.querySelectorAll(".stream-row");
    const streams = [];
    
    let isValid = true;
    rows.forEach(r => {
        const name = r.querySelector(".input-name").value.trim();
        const url = r.querySelector(".input-url").value.trim();
        const protocol = r.querySelector(".input-protocol").value.trim() || "tcp";
        
        if (!name || !url) {
            alert("Stream Name and RTSP URL are required.");
            isValid = false;
            return;
        }
        
        streams.push({
            stream_name: name,
            account_id: r.querySelector('.input-account').value || 'default',
            fallback_account_ids: Array.from(r.querySelector('.input-fallbacks').selectedOptions, o => o.value),
            url: url,
            protocol: protocol
        });
    });
    
    if (!isValid) return;
    
    try {
        const response = await fetch("api/streams/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ streams })
        });
        
        if (!response.ok) throw new Error("Failed to save configuration");
        
        showToast("Streams configuration saved successfully!");
        closeStreamsModal();
        refreshStatus();
    } catch (e) {
        alert(`Error saving configuration: ${e.message}`);
    }
}
