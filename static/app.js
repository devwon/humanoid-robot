/**
 * Remote CLI Bridge - Main Application
 * Remote access to Claude Code via voice and text.
 */

class WSClient {
    constructor(url) {
        this.url = url;
        this.ws = null;
        this.sessionId = localStorage.getItem('claude_session_id') || null;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 15;
        this.onMessage = null;
        this.onStatusChange = null;
    }

    connect() {
        try {
            this.ws = new WebSocket(this.url);
        } catch (e) {
            if (this.onStatusChange) this.onStatusChange('error');
            this._reconnect();
            return;
        }

        this.ws.onopen = () => {
            this.reconnectAttempts = 0;
            if (this.onStatusChange) this.onStatusChange('connected');
        };

        this.ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                if (msg.session_id) {
                    this.sessionId = msg.session_id;
                    localStorage.setItem('claude_session_id', msg.session_id);
                }
                if (this.onMessage) this.onMessage(msg);
            } catch (e) {
                console.error('Parse error:', e);
            }
        };

        this.ws.onclose = () => {
            if (this.onStatusChange) this.onStatusChange('disconnected');
            this._reconnect();
        };

        this.ws.onerror = () => {
            if (this.onStatusChange) this.onStatusChange('error');
        };
    }

    send(type, content, extra) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            console.warn('[WS] not open, dropping:', type);
            return false;
        }
        const msg = { type, content, session_id: this.sessionId, ...extra };
        this.ws.send(JSON.stringify(msg));
        return true;
    }

    interrupt() {
        return this.send('interrupt', '');
    }

    newSession() {
        this.sessionId = null;
        localStorage.removeItem('claude_session_id');
    }

    _reconnect() {
        if (this.reconnectAttempts >= this.maxReconnectAttempts) return;
        this.reconnectAttempts++;
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), 30000);
        setTimeout(() => this.connect(), delay);
    }
}


class App {
    constructor() {
        this.speech = new SpeechManager();
        const wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.ws = new WSClient(`${wsProto}//${location.host}/ws`);
        this.mode = 'claude'; // 'claude' | 'terminal' | 'robot'
        this.isVoiceMode = false;
        this.isProcessing = false;
        this.accumulatedResponse = '';
        this.currentResponseEl = null;
        this.autoSpeak = true;

        // Terminal state
        this.tmuxTarget = '';
        this._autoRefreshTimer = null;
        this._promptWatchTimer = null;
        this._lastPromptHash = '';

        // Robot state
        this._robotConnected = false;
        this._robotStateTimer = null;
        this._joystickTouching = false;

        this._cacheElements();
        this._bindEvents();
    }

    _cacheElements() {
        this.els = {
            messages: document.getElementById('messages'),
            termOutput: document.getElementById('terminal-output'),
            input: document.getElementById('text-input'),
            sendBtn: document.getElementById('send-btn'),
            micBtn: document.getElementById('mic-btn'),
            stopBtn: document.getElementById('stop-btn'),
            newBtn: document.getElementById('new-btn'),
            status: document.getElementById('connection-status'),
            interim: document.getElementById('interim-text'),
            langSelect: document.getElementById('lang-select'),
            speakToggle: document.getElementById('speak-toggle'),
            wakeWordToggle: document.getElementById('wakeword-toggle'),
            wakeIndicator: document.getElementById('wake-indicator'),
            claudeSettings: document.getElementById('claude-settings'),
            termToolbar: document.getElementById('terminal-toolbar'),
            tmuxSelect: document.getElementById('tmux-session-select'),
            tmuxRefreshBtn: document.getElementById('tmux-refresh-btn'),
            tmuxNewBtn: document.getElementById('tmux-new-btn'),
            tmuxAutoRefresh: document.getElementById('tmux-auto-refresh'),
            termKeybar: document.getElementById('terminal-keybar'),
            // Robot
            robotPane: document.getElementById('robot-pane'),
            robotToolbar: document.getElementById('robot-toolbar'),
            robotConnectBtn: document.getElementById('robot-connect-btn'),
            robotStatusLed: document.getElementById('robot-status-led'),
            robotCamFront: document.getElementById('robot-cam-front'),
            robotCamTop: document.getElementById('robot-cam-top'),
            robotJoystickL: document.getElementById('robot-joystick-l'),
            robotJoystickR: document.getElementById('robot-joystick-r'),
            robotCameraSelect: document.getElementById('robot-camera-select'),
            robotSpeed: document.getElementById('robot-speed'),
            robotSliders: document.getElementById('robot-sliders'),
            robotStopBtn: document.getElementById('robot-stop-btn'),
            robotGripper: document.getElementById('robot-gripper'),
            robotGripperVal: document.getElementById('robot-gripper-val'),
            robotWristFlex: document.getElementById('robot-wrist-flex'),
            robotWristFlexVal: document.getElementById('robot-wrist-flex-val'),
        };
    }

    _bindEvents() {
        // WebSocket
        this.ws.onMessage = (msg) => this._handleMessage(msg);
        this.ws.onStatusChange = (s) => this._updateStatus(s);

        // Speech recognition
        this.speech.onResult = (transcript, isFinal) => {
            if (isFinal) {
                this.els.interim.textContent = '';
                if (this.mode === 'claude') {
                    this._sendClaude(transcript, 'user_voice');
                } else if (this.mode === 'robot') {
                    this._sendRobotVoice(transcript);
                } else {
                    this._sendTerminal(transcript);
                }
            } else {
                this.els.interim.textContent = transcript;
            }
        };
        this.speech.onListeningChange = (listening) => {
            this.els.micBtn.classList.toggle('active', listening);
        };
        this.speech.onError = (err) => {
            this._addSystemMessage(`Mic error: ${err}`);
        };

        // UI events
        this.els.sendBtn.addEventListener('click', () => this._onSend());
        this.els.input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                this._onSend();
            }
        });
        this.els.micBtn.addEventListener('click', () => this._toggleVoice());
        this.els.stopBtn.addEventListener('click', () => this._onStop());
        this.els.newBtn.addEventListener('click', () => this._onNewSession());
        this.els.langSelect.addEventListener('change', (e) => {
            this.speech.setLanguage(e.target.value);
        });
        this.els.speakToggle.addEventListener('change', (e) => {
            this.autoSpeak = e.target.checked;
        });
        this.els.wakeWordToggle.addEventListener('change', (e) => {
            this._toggleWakeWord(e.target.checked);
        });

        // Wake word callbacks
        this.speech.onActivationChange = (activated) => {
            this._updateWakeIndicator(activated);
        };

        // Mode tabs
        document.querySelectorAll('.mode-tab').forEach(tab => {
            tab.addEventListener('click', () => this._switchMode(tab.dataset.mode));
        });

        // Terminal toolbar
        this.els.tmuxSelect.addEventListener('change', (e) => {
            this.tmuxTarget = e.target.value;
            if (this.tmuxTarget) this._termCapture();
        });
        this.els.tmuxRefreshBtn.addEventListener('click', () => this._termRefreshSessions());
        this.els.tmuxNewBtn.addEventListener('click', () => {
            const name = prompt('Session name (leave empty for auto):') || '';
            this.ws.send('terminal_create', name);
        });
        this.els.tmuxAutoRefresh.addEventListener('change', (e) => {
            if (e.target.checked && this.tmuxTarget) {
                this._startAutoRefresh();
            } else {
                this._stopAutoRefresh();
            }
        });

        // Terminal key buttons
        document.querySelectorAll('.key-btn:not(.robot-preset):not(.robot-stop-btn)').forEach(btn => {
            btn.addEventListener('mousedown', (e) => e.preventDefault());
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                this._sendTerminalKey(btn.dataset.key);
            });
        });

        // Robot events
        this.els.robotConnectBtn.addEventListener('click', () => this._robotToggleConnect());
        this.els.robotStopBtn.addEventListener('click', () => this.ws.send('robot_stop', ''));
        this.els.robotCameraSelect.addEventListener('change', (e) => this._robotUpdateCameraView(e.target.value));

        document.querySelectorAll('.robot-preset').forEach(btn => {
            btn.addEventListener('click', () => {
                const action = btn.dataset.action;
                if (action === 'home') this.ws.send('robot_home', '');
                else if (action === 'grab') this.ws.send('robot_action', JSON.stringify({'gripper.pos': 100}));
                else if (action === 'release') this.ws.send('robot_action', JSON.stringify({'gripper.pos': -100}));
            });
        });

        // Robot detail sliders
        this.els.robotSliders.querySelectorAll('.robot-slider-row').forEach(row => {
            const joint = row.dataset.joint;
            const input = row.querySelector('input');
            const val = row.querySelector('.slider-val');
            input.addEventListener('input', () => {
                val.textContent = input.value;
                this.ws.send('robot_action', JSON.stringify({[joint + '.pos']: Number(input.value)}));
            });
        });

        // Gripper slider
        this.els.robotGripper.addEventListener('input', () => {
            const v = Number(this.els.robotGripper.value);
            this.els.robotGripperVal.textContent = v;
            this.ws.send('robot_action', JSON.stringify({'gripper.pos': v}));
        });

        // Wrist flex slider
        this.els.robotWristFlex.addEventListener('input', () => {
            const v = Number(this.els.robotWristFlex.value);
            this.els.robotWristFlexVal.textContent = v;
            this.ws.send('robot_action', JSON.stringify({'wrist_flex.pos': v}));
        });

        // Robot joysticks (L = base rotation + shoulder up/down, R = elbow extend/retract + wrist)
        this._initJoystick(this.els.robotJoystickL, 'shoulder_pan.pos', 'shoulder_lift.pos',
            ['← L', 'R →'], ['↑ Up', '↓ Dn'], -1, -1);
        this._initJoystick(this.els.robotJoystickR, 'wrist_roll.pos', 'elbow_flex.pos',
            ['↶ Roll', 'Roll ↷'], ['↑ Fwd', '↓ Back'], -1, -1);
    }

    // ---- Mode switching ----

    _switchMode(mode) {
        this.mode = mode;
        document.querySelectorAll('.mode-tab').forEach(t => {
            t.classList.toggle('active', t.dataset.mode === mode);
        });

        // Claude
        this.els.messages.style.display = mode === 'claude' ? 'flex' : 'none';
        this.els.claudeSettings.style.display = mode === 'claude' ? 'flex' : 'none';
        // Terminal
        this.els.termOutput.style.display = mode === 'terminal' ? 'block' : 'none';
        this.els.termToolbar.style.display = mode === 'terminal' ? 'flex' : 'none';
        if (this.els.termKeybar) this.els.termKeybar.style.display = mode === 'terminal' ? 'flex' : 'none';
        // Robot
        this.els.robotPane.style.display = mode === 'robot' ? 'flex' : 'none';
        this.els.robotToolbar.style.display = mode === 'robot' ? 'flex' : 'none';

        const placeholders = { claude: 'Type or speak a message...', terminal: 'Enter command...', robot: 'Voice command...' };
        this.els.input.placeholder = placeholders[mode] || '';

        if (mode === 'terminal') {
            this._termRefreshSessions();
        } else {
            this._stopAutoRefresh();
        }
        if (mode === 'robot') {
            requestAnimationFrame(() => this._robotRedrawJoysticks());
            this._robotStartCameras();
            if (!this._robotConnected) this._robotToggleConnect();
        } else {
            this._robotStopCameras();
        }
    }

    // ---- Send dispatcher ----

    _onSend() {
        const text = this.els.input.value.trim();
        if (!text) return;
        this.els.input.value = '';

        if (this.mode === 'claude') {
            this._sendClaude(text, 'user_text');
        } else if (this.mode === 'robot') {
            this._sendRobotVoice(text);
        } else {
            this._sendTerminal(text);
        }
    }

    // ---- Claude mode ----

    _sendClaude(text, type) {
        this._addUserMessage(text);
        this.ws.send(type, text);
        this.isProcessing = true;
        this.accumulatedResponse = '';
        this._updateButtons();
    }

    _onStop() {
        this.ws.interrupt();
        this.speech.stopSpeaking();
        this.isProcessing = false;
        this._updateButtons();
    }

    _onNewSession() {
        if (this.mode === 'claude') {
            this.ws.newSession();
            this.els.messages.innerHTML = '';
            this._addSystemMessage('New session started');
        }
    }

    _toggleVoice() {
        if (this.isVoiceMode) {
            this.speech.stopListening();
            this.isVoiceMode = false;
        } else {
            this.isVoiceMode = this.speech.startListening();
            if (!this.isVoiceMode) {
                this._addSystemMessage('Speech recognition not available. Check HTTPS and mic permissions.');
            }
        }
    }

    // ---- Wake word ----

    _toggleWakeWord(enabled) {
        this.speech.wakeWordMode = enabled;
        if (enabled) {
            this.speech.startListening();
            this.isVoiceMode = true;
            this._updateWakeIndicator(false);
        } else {
            this.speech._deactivate();
            this.speech.stopListening();
            this.isVoiceMode = false;
            if (this.els.wakeIndicator) this.els.wakeIndicator.className = 'wake-indicator';
        }
    }

    _updateWakeIndicator(activated) {
        if (!this.speech.wakeWordMode || !this.els.wakeIndicator) return;
        if (activated) {
            this.els.wakeIndicator.textContent = 'Listening...';
            this.els.wakeIndicator.className = 'wake-indicator active';
        } else {
            this.els.wakeIndicator.textContent = '"클로드" 대기 중...';
            this.els.wakeIndicator.className = 'wake-indicator waiting';
        }
    }

    // ---- Terminal mode ----

    // Voice → key mapping
    _voiceKeyMap = {
        '위': 'Up', '업': 'Up', '위로': 'Up', '이전': 'Up',
        '아래': 'Down', '다운': 'Down', '아래로': 'Down', '다음': 'Down',
        '왼쪽': 'Left', '레프트': 'Left',
        '오른쪽': 'Right', '라이트': 'Right',
        '엔터': 'Enter', '확인': 'Enter', '실행': 'Enter', '선택': 'Enter',
        '탭': 'Tab', '자동완성': 'Tab',
        '취소': 'Escape', '에스케이프': 'Escape', 'esc': 'Escape',
        '중단': 'C-c', '컨트롤씨': 'C-c', '컨트롤 씨': 'C-c', '인터럽트': 'C-c',
        '컨트롤디': 'C-d', '컨트롤 디': 'C-d',
        '컨트롤제트': 'C-z', '컨트롤 제트': 'C-z',
        '스페이스': 'Space', '공백': 'Space',
        '예': 'y', '와이': 'y', '네': 'y',
        '아니': 'n', '아니오': 'n', '엔': 'n',
    };

    _sendTerminal(cmd) {
        if (!this.tmuxTarget) {
            this._termAddLine(`[No session selected]`, 'term-error');
            return;
        }
        // Check voice key mapping
        const key = this._voiceKeyMap[cmd.trim().toLowerCase()];
        if (key) {
            this._sendTerminalKey(key);
            return;
        }
        this._termAddLine(`$ ${cmd}`, 'term-cmd');
        this.ws.send('terminal_send', cmd, { target: this.tmuxTarget });
        this._startPromptWatch();
    }

    _sendTerminalKey(key) {
        if (!this.tmuxTarget) {
            this._termAddLine(`[No session selected]`, 'term-error');
            return;
        }
        this._termAddLine(`[key: ${key}]`, 'term-cmd');
        this.ws.send('terminal_key', key, { target: this.tmuxTarget });
        this._startPromptWatch();
    }

    _termCapture() {
        if (!this.tmuxTarget) return;
        this.ws.send('terminal_capture', this.tmuxTarget);
    }

    _termRefreshSessions() {
        this.ws.send('terminal_list', '');
    }

    _termUpdateSessionList(sessions) {
        const sel = this.els.tmuxSelect;
        const prev = sel.value;
        sel.innerHTML = '<option value="">-- select session --</option>';
        for (const s of sessions) {
            const opt = document.createElement('option');
            opt.value = `${s.name}:0`;
            opt.textContent = `${s.name} (${s.windows}w${s.attached ? ', attached' : ''})`;
            sel.appendChild(opt);
        }
        // Restore previous selection if still valid
        if (prev && [...sel.options].some(o => o.value === prev)) {
            sel.value = prev;
            this.tmuxTarget = prev;
        }
    }

    _termShowOutput(text) {
        this.els.termOutput.innerHTML = this._ansiToHtml(text);
        this.els.termOutput.scrollTop = this.els.termOutput.scrollHeight;
        this._checkForPrompt(text);
    }

    // Prompt detection & notification
    _promptPatterns = [
        /\[Y\/n\]/i, /\[y\/N\]/i, /\(y\/n\)/i, /\(yes\/no\)/i,
        /❯|▸|►/,
        /◉.*○|○.*◉/,
        /\? ›/,
        /Press (enter|any key|return|space)/i,
        /Continue\?/i, /Proceed\?/i, /Overwrite\?/i, /Delete\?/i, /Replace\?/i,
        /Are you sure/i,
        /\(default[^)]*\)\s*[:?]?\s*$/m,
    ];

    _checkForPrompt(text) {
        const lines = text.split('\n').filter(l => l.trim());
        const tail = lines.slice(-5).join('\n');
        const clean = tail.replace(/\x1b\[[0-9;]*m/g, '');

        const isPrompt = this._promptPatterns.some(p => p.test(clean));
        if (!isPrompt) return;

        if (this._lastPromptHash === clean) return;
        this._lastPromptHash = clean;

        this._notifyTerminalPrompt();
    }

    _notifyTerminalPrompt() {
        // Double beep (ascending) through glasses speakers
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const tone = (freq, start, dur) => {
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.frequency.value = freq;
                osc.type = 'sine';
                gain.gain.value = 0.3;
                gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + start + dur);
                osc.start(ctx.currentTime + start);
                osc.stop(ctx.currentTime + start + dur);
            };
            tone(600, 0, 0.12);
            tone(900, 0.18, 0.12);
            setTimeout(() => ctx.close(), 1000);
        } catch (e) {}

        // Vibrate on mobile
        if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
    }

    _startPromptWatch() {
        this._stopPromptWatch();
        let checks = 0;
        this._promptWatchTimer = setInterval(() => {
            if (++checks > 15) { this._stopPromptWatch(); return; }
            this._termCapture();
        }, 2000);
    }

    _stopPromptWatch() {
        if (this._promptWatchTimer) {
            clearInterval(this._promptWatchTimer);
            this._promptWatchTimer = null;
        }
    }

    // ANSI escape code → HTML converter
    _ansiColors = [
        '#000','#c23621','#25bc24','#adad27','#492ee1','#d338d3','#33bbc8','#cbcccd',  // 0-7
        '#818383','#fc391f','#31e722','#eaec23','#5833ff','#f935f8','#14f0f0','#e9ebeb', // 8-15 (bright)
    ];

    _ansiToHtml(text) {
        // HTML escape first
        let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

        let fg = null, bg = null, bold = false, dim = false, italic = false, underline = false;
        let result = '';
        let spanOpen = false;

        const parts = html.split(/(\x1b\[[0-9;]*m)/);
        for (const part of parts) {
            const match = part.match(/^\x1b\[([0-9;]*)m$/);
            if (!match) {
                if (part) {
                    if (fg !== null || bg !== null || bold || dim || italic || underline) {
                        const styles = [];
                        if (fg !== null) styles.push(`color:${fg}`);
                        if (bg !== null) styles.push(`background:${bg}`);
                        if (bold) styles.push('font-weight:bold');
                        if (dim) styles.push('opacity:0.6');
                        if (italic) styles.push('font-style:italic');
                        if (underline) styles.push('text-decoration:underline');
                        result += `<span style="${styles.join(';')}">${part}</span>`;
                    } else {
                        result += part;
                    }
                }
                continue;
            }

            const codes = match[1] ? match[1].split(';').map(Number) : [0];
            for (let i = 0; i < codes.length; i++) {
                const c = codes[i];
                if (c === 0) { fg = null; bg = null; bold = false; dim = false; italic = false; underline = false; }
                else if (c === 1) bold = true;
                else if (c === 2) dim = true;
                else if (c === 3) italic = true;
                else if (c === 4) underline = true;
                else if (c === 22) { bold = false; dim = false; }
                else if (c === 23) italic = false;
                else if (c === 24) underline = false;
                else if (c === 39) fg = null;
                else if (c === 49) bg = null;
                else if (c >= 30 && c <= 37) fg = this._ansiColors[bold ? c - 30 + 8 : c - 30];
                else if (c >= 40 && c <= 47) bg = this._ansiColors[c - 40];
                else if (c >= 90 && c <= 97) fg = this._ansiColors[c - 90 + 8];
                else if (c >= 100 && c <= 107) bg = this._ansiColors[c - 100 + 8];
                else if (c === 38 && codes[i+1] === 5) { fg = this._color256(codes[i+2] || 0); i += 2; }
                else if (c === 48 && codes[i+1] === 5) { bg = this._color256(codes[i+2] || 0); i += 2; }
            }
        }
        return result;
    }

    _color256(n) {
        if (n < 16) return this._ansiColors[n];
        if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
        n -= 16;
        const r = Math.floor(n / 36) * 51, g = Math.floor((n % 36) / 6) * 51, b = (n % 6) * 51;
        return `rgb(${r},${g},${b})`;
    }

    _termAddLine(text, cls) {
        const line = document.createElement('div');
        line.className = cls || '';
        line.textContent = text;
        this.els.termOutput.appendChild(line);
        this.els.termOutput.scrollTop = this.els.termOutput.scrollHeight;
    }

    _startAutoRefresh() {
        this._stopAutoRefresh();
        this._autoRefreshTimer = setInterval(() => this._termCapture(), 2000);
    }

    _stopAutoRefresh() {
        if (this._autoRefreshTimer) {
            clearInterval(this._autoRefreshTimer);
            this._autoRefreshTimer = null;
        }
    }

    // ---- Message handling ----

    _handleMessage(msg) {
        switch (msg.type) {
            // Claude messages
            case 'assistant_chunk':
                this._appendChunk(msg.content);
                break;
            case 'assistant_done':
                this._finalizeResponse(msg);
                break;
            case 'tool_use':
                this._addToolMessage(msg.content, 'tool-use');
                break;
            case 'tool_result':
                this._addToolMessage(msg.content, msg.metadata?.is_error ? 'tool-error' : 'tool-result');
                break;
            case 'status':
                this._updateStatusText(msg.content);
                break;
            case 'error':
                if (this.mode === 'terminal') {
                    this._termAddLine(msg.content, 'term-error');
                } else if (this.mode === 'robot') {
                    this._addSystemMessage(`Robot error: ${msg.content}`);
                } else {
                    this._addSystemMessage(`Error: ${msg.content}`);
                }
                this.isProcessing = false;
                this._updateButtons();
                break;
            case 'session_info':
                break;

            // Terminal messages
            case 'terminal_sessions':
                this._termUpdateSessionList(JSON.parse(msg.content));
                break;
            case 'terminal_windows':
                break;
            case 'terminal_output':
                this._termShowOutput(msg.content);
                break;

            // Robot messages
            case 'robot_connected':
                this._robotOnConnected();
                break;
            case 'robot_disconnected':
                this._robotOnDisconnected();
                break;
            case 'robot_state':
                this._robotUpdateState(msg.content);
                break;
            case 'robot_error':
                this._addSystemMessage(`Robot: ${msg.content}`);
                break;
        }
    }

    _appendChunk(text) {
        if (!this.currentResponseEl) {
            this.currentResponseEl = this._createMessageEl('assistant');
            this.currentResponseEl.querySelector('.msg-content').textContent = '';
        }
        const contentEl = this.currentResponseEl.querySelector('.msg-content');
        contentEl.textContent += text;
        this.accumulatedResponse += text;
        this._scrollToBottom();

        if (this.autoSpeak) {
            this._speakCompletedSentences();
        }
    }

    _speakCompletedSentences() {
        const endPattern = /[.!?\n]\s*/g;
        let match;
        let lastIndex = 0;
        while ((match = endPattern.exec(this.accumulatedResponse)) !== null) {
            const sentence = this.accumulatedResponse.slice(lastIndex, match.index + match[0].length).trim();
            if (sentence.length > 3) {
                this.speech.speak(sentence);
            }
            lastIndex = match.index + match[0].length;
        }
        if (lastIndex > 0) {
            this.accumulatedResponse = this.accumulatedResponse.slice(lastIndex);
        }
    }

    _finalizeResponse(msg) {
        if (this.autoSpeak && this.accumulatedResponse.trim()) {
            this.speech.speak(this.accumulatedResponse);
        }

        if (this.currentResponseEl && msg.metadata) {
            const meta = msg.metadata;
            const parts = [];

            const inTok = (meta.input_tokens || 0) + (meta.cache_read_tokens || 0) + (meta.cache_creation_tokens || 0);
            const outTok = meta.output_tokens || 0;
            if (inTok || outTok) parts.push(`${this._formatTokens(inTok)} in / ${this._formatTokens(outTok)} out`);

            if (meta.model) parts.push(meta.model);

            if (meta.cost_usd) {
                let costStr = `$${Number(meta.cost_usd).toFixed(4)}`;
                if (meta.cost_krw != null) {
                    costStr += ` (${Number(meta.cost_krw).toLocaleString()}원)`;
                }
                parts.push(costStr);
            }

            if (meta.duration_ms) parts.push(`${(meta.duration_ms / 1000).toFixed(1)}s`);

            if (parts.length > 0) {
                const metaEl = document.createElement('div');
                metaEl.className = 'msg-meta';
                metaEl.textContent = parts.join('  ·  ');
                this.currentResponseEl.appendChild(metaEl);
            }
        }

        this.currentResponseEl = null;
        this.accumulatedResponse = '';
        this.isProcessing = false;
        this._updateButtons();
    }

    _formatTokens(n) {
        if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
        return String(n);
    }

    _addUserMessage(text) {
        const el = this._createMessageEl('user');
        el.querySelector('.msg-content').textContent = text;
        this._scrollToBottom();
    }

    _addSystemMessage(text) {
        const el = document.createElement('div');
        el.className = 'message system';
        el.textContent = text;
        this.els.messages.appendChild(el);
        this._scrollToBottom();
    }

    _addToolMessage(text, className) {
        const el = document.createElement('div');
        el.className = `message tool ${className}`;
        el.textContent = text;
        this.els.messages.appendChild(el);
        this._scrollToBottom();
    }

    _createMessageEl(role) {
        const el = document.createElement('div');
        el.className = `message ${role}`;

        const header = document.createElement('div');
        header.className = 'msg-header';

        const label = document.createElement('span');
        label.className = 'msg-label';
        label.textContent = role === 'user' ? 'You' : 'Claude';

        const time = document.createElement('span');
        time.className = 'msg-time';
        time.textContent = this._formatTime(new Date());

        header.appendChild(label);
        header.appendChild(time);

        const content = document.createElement('div');
        content.className = 'msg-content';

        el.appendChild(header);
        el.appendChild(content);
        this.els.messages.appendChild(el);
        return el;
    }

    _formatTime(date) {
        const mm = String(date.getMonth() + 1).padStart(2, '0');
        const dd = String(date.getDate()).padStart(2, '0');
        const hh = String(date.getHours()).padStart(2, '0');
        const mi = String(date.getMinutes()).padStart(2, '0');
        return `${mm}/${dd} ${hh}:${mi}`;
    }

    _updateStatus(status) {
        this.els.status.textContent = status;
        this.els.status.className = `status ${status}`;
        // Reset stuck processing state on reconnect or disconnect
        if (status === 'connected' || status === 'disconnected' || status === 'error') {
            if (this.isProcessing) {
                this.isProcessing = false;
                this._updateButtons();
            }
        }
    }

    _updateStatusText(text) {
        this.els.status.textContent = text;
        setTimeout(() => {
            if (this.els.status.textContent === text) {
                this.els.status.textContent = 'connected';
                this.els.status.className = 'status connected';
            }
        }, 3000);
    }

    _updateButtons() {
        this.els.sendBtn.disabled = this.isProcessing;
        this.els.stopBtn.style.display = this.isProcessing ? 'flex' : 'none';
    }

    _scrollToBottom() {
        this.els.messages.scrollTop = this.els.messages.scrollHeight;
    }

    // ---- Robot mode ----

    _robotVoiceMap = {
        '홈': 'home', '정지': 'stop', '멈춰': 'stop', '스톱': 'stop', 'stop': 'stop',
        '잡아': 'grab', '그랩': 'grab', '집어': 'grab', 'grab': 'grab',
        '놓아': 'release', '릴리스': 'release', '놓아줘': 'release', 'release': 'release',
        'home': 'home',
    };

    _robotDirectionMap = {
        '왼쪽': { 'shoulder_pan.pos': -5 },
        '오른쪽': { 'shoulder_pan.pos': 5 },
        '위': { 'shoulder_lift.pos': -5 },
        '아래': { 'shoulder_lift.pos': 5 },
        'left': { 'shoulder_pan.pos': -5 },
        'right': { 'shoulder_pan.pos': 5 },
        'up': { 'shoulder_lift.pos': -5 },
        'down': { 'shoulder_lift.pos': 5 },
    };

    _sendRobotVoice(text) {
        const cmd = text.trim().toLowerCase();
        const preset = this._robotVoiceMap[cmd];
        if (preset === 'home') { this.ws.send('robot_home', ''); return; }
        if (preset === 'stop') { this.ws.send('robot_stop', ''); return; }
        if (preset === 'grab') { this.ws.send('robot_action', JSON.stringify({'gripper.pos': 100})); return; }
        if (preset === 'release') { this.ws.send('robot_action', JSON.stringify({'gripper.pos': -100})); return; }
        const dir = this._robotDirectionMap[cmd];
        if (dir) { this.ws.send('robot_action', JSON.stringify(dir), { subtype: 'delta' }); return; }
        // Unknown command - send to Claude for interpretation
        this._addSystemMessage(`Robot voice: "${text}"`);
    }

    _robotToggleConnect() {
        if (this._robotConnected) {
            this.ws.send('robot_disconnect', '');
            this.els.robotConnectBtn.textContent = 'Connecting...';
        } else {
            this.ws.send('robot_connect', '');
            this.els.robotConnectBtn.textContent = 'Connecting...';
        }
    }

    _robotOnConnected() {
        this._robotConnected = true;
        this.els.robotConnectBtn.textContent = 'Disconnect';
        this.els.robotConnectBtn.classList.add('connected');
        this.els.robotStatusLed.className = 'robot-led connected';
        if (this.mode === 'robot') this._robotStartCameras();
    }

    _robotOnDisconnected() {
        this._robotConnected = false;
        this.els.robotConnectBtn.textContent = 'Connect';
        this.els.robotConnectBtn.classList.remove('connected');
        this.els.robotStatusLed.className = 'robot-led';
        this._robotStopCameras();
    }

    _robotRedrawJoysticks() {
        // Trigger draw after robot pane becomes visible
        this.els.robotJoystickL?.dispatchEvent(new Event('_redraw'));
        this.els.robotJoystickR?.dispatchEvent(new Event('_redraw'));
    }

    _robotStartCameras() {
        this._robotStopCameras();
        if (!this._robotConnected) return;
        const base = `${location.protocol}//${location.host}/api/robot/camera`;
        const view = this.els.robotCameraSelect.value;

        const showFront = view === 'front' || view === 'both';
        const showTop = view === 'top' || view === 'both';
        this.els.robotCamFront.classList.toggle('hidden', !showFront);
        this.els.robotCamTop.classList.toggle('hidden', !showTop);
        this.els.robotCamFront.parentElement.classList.toggle('dual', showFront && showTop);

        this._camRunning = true;

        const pollCam = async (img, name) => {
            let prevUrl = null;
            while (this._camRunning) {
                try {
                    const resp = await fetch(`${base}/${name}?t=${Date.now()}`);
                    if (!this._camRunning) break;
                    if (resp.ok) {
                        const blob = await resp.blob();
                        if (!this._camRunning) break;
                        const url = URL.createObjectURL(blob);
                        img.src = url;
                        if (prevUrl) URL.revokeObjectURL(prevUrl);
                        prevUrl = url;
                    }
                } catch {
                    // Network error — wait longer before retry
                    await new Promise(r => setTimeout(r, 500));
                    continue;
                }
                await new Promise(r => setTimeout(r, 16));
            }
            if (prevUrl) URL.revokeObjectURL(prevUrl);
        };
        if (showFront) pollCam(this.els.robotCamFront, 'front');
        if (showTop) pollCam(this.els.robotCamTop, 'top');
    }

    _robotStopCameras() {
        this._camRunning = false;
        this.els.robotCamFront.src = '';
        this.els.robotCamTop.src = '';
    }

    _robotUpdateCameraView(view) {
        this._robotStartCameras();
    }

    _robotUpdateState(stateJson) {
        try {
            const state = typeof stateJson === 'string' ? JSON.parse(stateJson) : stateJson;
            const joints = state.joints || state;
            // Detail sliders
            this.els.robotSliders.querySelectorAll('.robot-slider-row').forEach(row => {
                const joint = row.dataset.joint;
                const key = joint + '.pos';
                if (key in joints) {
                    const val = Math.round(joints[key]);
                    const input = row.querySelector('input');
                    const display = row.querySelector('.slider-val');
                    if (document.activeElement !== input) input.value = val;
                    display.textContent = val;
                }
            });
            // Gripper bar
            if ('gripper.pos' in joints && document.activeElement !== this.els.robotGripper) {
                const v = Math.round(joints['gripper.pos']);
                this.els.robotGripper.value = v;
                this.els.robotGripperVal.textContent = v;
            }
            // Wrist flex bar
            if ('wrist_flex.pos' in joints && document.activeElement !== this.els.robotWristFlex) {
                const v = Math.round(joints['wrist_flex.pos']);
                this.els.robotWristFlex.value = v;
                this.els.robotWristFlexVal.textContent = v;
            }
        } catch (e) {}
    }

    _initJoystick(canvas, axisX, axisY, labelX, labelY, signX = 1, signY = 1) {
        if (!canvas) return;
        const ctx = canvas.getContext('2d');

        let knobX = 0, knobY = 0;
        let touching = false;
        let ready = false;

        const R = () => canvas.width / 2;
        const knobR = () => canvas.width * 0.22;

        // Resize canvas to match CSS size — only when visible
        const ensureReady = () => {
            const rect = canvas.getBoundingClientRect();
            if (rect.width < 10) return false;
            const s = Math.round(rect.width);
            if (canvas.width !== s) { canvas.width = s; canvas.height = s; }
            ready = true;
            return true;
        };

        const draw = () => {
            if (!ensureReady()) return;
            const r = R(), kr = knobR(), w = canvas.width;
            ctx.clearRect(0, 0, w, w);
            // Background
            ctx.beginPath();
            ctx.arc(r, r, r - 1, 0, Math.PI * 2);
            ctx.fillStyle = '#161b22';
            ctx.fill();
            // Subtle ring
            ctx.beginPath();
            ctx.arc(r, r, r - 1, 0, Math.PI * 2);
            ctx.strokeStyle = '#21262d';
            ctx.lineWidth = 1.5;
            ctx.stroke();

            // Axis labels (arrows + text)
            const labelColor = touching ? '#484f58' : '#30363d';
            const fontSize = Math.max(9, Math.round(w * 0.07));
            ctx.font = `${fontSize}px -apple-system, sans-serif`;
            ctx.fillStyle = labelColor;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            // X axis labels (left / right)
            if (labelX) {
                ctx.fillText(labelX[0], fontSize + 2, r);              // left label
                ctx.fillText(labelX[1], w - fontSize - 2, r);           // right label
            }
            // Y axis labels (up / down)
            if (labelY) {
                ctx.fillText(labelY[0], r, fontSize + 2);               // top label
                ctx.fillText(labelY[1], r, w - fontSize - 2);           // bottom label
            }

            // Knob position
            const maxDist = r - kr;
            const kx = r + knobX * maxDist;
            const ky = r + knobY * maxDist;
            // Knob
            ctx.beginPath();
            ctx.arc(kx, ky, kr, 0, Math.PI * 2);
            ctx.fillStyle = touching ? '#1f6feb' : '#30363d';
            ctx.fill();
            ctx.beginPath();
            ctx.arc(kx, ky, kr, 0, Math.PI * 2);
            ctx.strokeStyle = touching ? '#58a6ff' : '#484f58';
            ctx.lineWidth = 1.5;
            ctx.stroke();
            // Center dot on knob
            ctx.beginPath();
            ctx.arc(kx, ky, 3, 0, Math.PI * 2);
            ctx.fillStyle = touching ? '#79c0ff' : '#6e7681';
            ctx.fill();
        };

        // Exponential curve: small movements near center, big at edges
        const curve = (v) => {
            const sign = v < 0 ? -1 : 1;
            return sign * Math.pow(Math.abs(v), 1.8);
        };

        const getXY = (e) => {
            const rect = canvas.getBoundingClientRect();
            const touch = e.touches ? e.touches[0] : e;
            const r = rect.width / 2;
            const dx = (touch.clientX - rect.left - r) / (r - knobR());
            const dy = (touch.clientY - rect.top - r) / (r - knobR());
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist > 1) return [dx / dist, dy / dist];
            return [dx, dy];
        };

        const sendVelocity = () => {
            const deadZone = 0.12;
            const dx = Math.abs(knobX) < deadZone ? 0 : curve(knobX);
            const dy = Math.abs(knobY) < deadZone ? 0 : curve(knobY);
            const speed = Number(this.els.robotSpeed.value) || 3;
            // Speed scale: degrees per second (speed 1=30, 2=60, 5=150)
            const scale = speed * 30;
            const vel = {};
            vel[axisX] = Math.round(dx * signX * scale * 10) / 10;
            vel[axisY] = Math.round(dy * signY * scale * 10) / 10;
            this.ws.send('robot_velocity', JSON.stringify(vel));
        };

        const stopVelocity = () => {
            const vel = {};
            vel[axisX] = 0;
            vel[axisY] = 0;
            this.ws.send('robot_velocity', JSON.stringify(vel));
        };

        const onStart = (e) => {
            e.preventDefault();
            touching = true;
            [knobX, knobY] = getXY(e);
            draw();
            sendVelocity();
        };

        const onMove = (e) => {
            if (!touching) return;
            e.preventDefault();
            [knobX, knobY] = getXY(e);
            draw();
            sendVelocity();
        };

        const onEnd = () => {
            touching = false;
            knobX = 0; knobY = 0;
            draw();
            stopVelocity();
        };

        canvas.addEventListener('touchstart', onStart, { passive: false });
        canvas.addEventListener('touchmove', onMove, { passive: false });
        canvas.addEventListener('touchend', onEnd);
        canvas.addEventListener('touchcancel', onEnd);
        canvas.addEventListener('mousedown', onStart);
        canvas.addEventListener('mousemove', onMove);
        canvas.addEventListener('mouseup', onEnd);
        canvas.addEventListener('mouseleave', onEnd);
        canvas.addEventListener('_redraw', draw);

        draw(); // safe: ensureReady() guards against hidden canvas
    }

    start() {
        this.ws.connect();
        if (!this.speech.isSupported) {
            this.els.micBtn.style.opacity = '0.3';
            this.els.micBtn.title = 'Speech not supported';
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const app = new App();
    app.start();
});
