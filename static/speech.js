/**
 * Web Speech API wrapper for voice input (STT) and output (TTS).
 * Designed for use with Ray-Ban Meta glasses as Bluetooth audio device.
 */
class SpeechManager {
    constructor() {
        this.recognition = null;
        this.synthesis = window.speechSynthesis;
        this.isListening = false;
        this.isSpeaking = false;
        this.lang = 'ko-KR';
        this.speechRate = 1.1;
        this.selectedVoice = null;
        this._speechQueue = [];
        this._currentUtterance = null;

        // Wake word
        this.wakeWordMode = false;
        this._activated = false;
        this._earlyWakeDetected = false;
        this._activationTimer = null;
        this._activationTimeout = 10000; // 10s
        this._wakeWordPatterns = [
            /^(?:헤이\s+)?클로드[\s,.!?]*/,
            /^(?:헤이\s+)?클라우드[\s,.!?]*/,
            /^(?:hey\s+)?claude[\s,.!?]*/i,
        ];

        // Callbacks
        this.onResult = null;       // (transcript, isFinal) => void
        this.onListeningChange = null; // (isListening) => void
        this.onError = null;        // (error) => void
        this.onActivationChange = null; // (activated) => void

        this._initRecognition();
        this._loadVoices();
    }

    get isSupported() {
        return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
    }

    _initRecognition() {
        const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SR) return;

        this.recognition = new SR();
        this.recognition.continuous = true;
        this.recognition.interimResults = true;
        this.recognition.lang = this.lang;

        this.recognition.onresult = (event) => {
            const last = event.results[event.results.length - 1];
            const transcript = last[0].transcript;
            const isFinal = last.isFinal;

            if (!isFinal) {
                // Always show interim text so user knows STT is working
                if (this.onResult) this.onResult(transcript, false);
                // Early wake word visual feedback (vibration only, no beep)
                if (this.wakeWordMode && !this._activated) {
                    const { found } = this._checkWakeWord(transcript);
                    if (found && !this._earlyWakeDetected) {
                        this._earlyWakeDetected = true;
                        if (navigator.vibrate) navigator.vibrate(100);
                        if (this.onActivationChange) this.onActivationChange(true);
                    } else if (!found && this._earlyWakeDetected) {
                        this._earlyWakeDetected = false;
                        if (this.onActivationChange) this.onActivationChange(false);
                    }
                }
                return;
            }

            // Final result
            if (this.wakeWordMode) {
                this._handleWakeWordFinal(transcript);
            } else {
                if (this.onResult) this.onResult(transcript, true);
            }
        };

        this.recognition.onerror = (event) => {
            // 'no-speech' and 'aborted' are not real errors
            if (event.error === 'no-speech' || event.error === 'aborted') return;
            if (this.onError) this.onError(event.error);
        };

        this.recognition.onend = () => {
            // Auto-restart if still in listening mode
            if (this.isListening) {
                try {
                    this.recognition.start();
                } catch (e) {
                    // Already started
                }
            }
        };
    }

    _loadVoices() {
        const loadFn = () => {
            const voices = this.synthesis.getVoices();
            // Prefer Korean voice, fallback to default
            this.selectedVoice =
                voices.find(v => v.lang.startsWith('ko') && v.localService) ||
                voices.find(v => v.lang.startsWith('ko')) ||
                voices.find(v => v.default) ||
                voices[0] || null;
        };
        loadFn();
        this.synthesis.onvoiceschanged = loadFn;
    }

    setLanguage(lang) {
        this.lang = lang;
        if (this.recognition) {
            this.recognition.lang = lang;
        }
        // Re-select voice for new language
        const voices = this.synthesis.getVoices();
        const prefix = lang.split('-')[0];
        this.selectedVoice =
            voices.find(v => v.lang.startsWith(prefix) && v.localService) ||
            voices.find(v => v.lang.startsWith(prefix)) ||
            this.selectedVoice;
    }

    startListening() {
        if (!this.recognition) return false;
        this.isListening = true;
        try {
            this.recognition.start();
            if (this.onListeningChange) this.onListeningChange(true);
            return true;
        } catch (e) {
            return false;
        }
    }

    stopListening() {
        this.isListening = false;
        if (this.recognition) {
            this.recognition.stop();
        }
        if (this.onListeningChange) this.onListeningChange(false);
    }

    /**
     * Speak text through the glasses speakers.
     * Splits into sentences for natural pacing.
     */
    speak(text) {
        if (!text || !text.trim()) return;

        // Skip code blocks - just announce them
        const codeBlockRegex = /```[\s\S]*?```/g;
        const cleaned = text.replace(codeBlockRegex, ' (code block omitted) ');

        // Split into sentences
        const sentences = cleaned.match(/[^.!?\n]+[.!?\n]+|[^.!?\n]+$/g) || [cleaned];
        for (const sentence of sentences) {
            const trimmed = sentence.trim();
            if (trimmed.length > 2) {
                this._speechQueue.push(trimmed);
            }
        }
        this._processQueue();
    }

    _processQueue() {
        if (this.isSpeaking || this._speechQueue.length === 0) return;

        this.isSpeaking = true;
        const text = this._speechQueue.shift();
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.voice = this.selectedVoice;
        utterance.rate = this.speechRate;
        utterance.lang = this.lang;
        this._currentUtterance = utterance;

        utterance.onend = () => {
            this.isSpeaking = false;
            this._currentUtterance = null;
            this._processQueue();
        };

        utterance.onerror = () => {
            this.isSpeaking = false;
            this._currentUtterance = null;
            this._processQueue();
        };

        this.synthesis.speak(utterance);
    }

    stopSpeaking() {
        this._speechQueue = [];
        this.synthesis.cancel();
        this.isSpeaking = false;
        this._currentUtterance = null;
    }

    getVoices() {
        return this.synthesis.getVoices();
    }

    // ---- Wake word ----

    _handleWakeWordFinal(transcript) {
        this._earlyWakeDetected = false;

        if (this._activated) {
            // Was activated by previous "클로드" → send this as command
            this._deactivate();
            this._playBeep();
            if (this.onResult) this.onResult(transcript, true);
            return;
        }

        const { found, command } = this._checkWakeWord(transcript);
        if (!found) return; // No wake word → ignore

        if (command) {
            // "클로드 날씨 알려줘" → send "날씨 알려줘"
            this._playBeep();
            if (this.onResult) this.onResult(command, true);
            if (this.onActivationChange) this.onActivationChange(false);
        } else {
            // Just "클로드" → activate and wait for next utterance
            this._activate();
        }
    }

    _checkWakeWord(transcript) {
        const text = transcript.trim();
        const lower = text.toLowerCase();
        for (const pattern of this._wakeWordPatterns) {
            const match = lower.match(pattern);
            if (match) {
                return { found: true, command: text.slice(match[0].length).trim() };
            }
        }
        return { found: false, command: '' };
    }

    _activate(skipBeep = false) {
        this._activated = true;
        if (!skipBeep) this._playBeep();
        if (this.onActivationChange) this.onActivationChange(true);
        this._activationTimer = setTimeout(() => {
            this._deactivate();
            this._playBeep(400, 0.1);
        }, this._activationTimeout);
    }

    _deactivate() {
        this._activated = false;
        clearTimeout(this._activationTimer);
        if (this.onActivationChange) this.onActivationChange(false);
    }

    _playBeep(freq = 800, duration = 0.15) {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.frequency.value = freq;
            osc.type = 'sine';
            gain.gain.value = 0.3;
            gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + duration);
            osc.start(ctx.currentTime);
            osc.stop(ctx.currentTime + duration);
            setTimeout(() => ctx.close(), 500);
        } catch (e) {}
    }
}
