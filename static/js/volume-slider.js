/**
 * HUMO O'quv Markazi - Phone Volume Slider Component
 * iOS / Android uslubidagi interaktiv audio-vizual shkala
 */
class PhoneVolumeSlider {
    constructor(elementId, options = {}) {
        this.container = document.getElementById(elementId);
        if (!this.container) return;

        this.input = this.container.querySelector('input[type="hidden"]');
        this.capsule = this.container.querySelector('.phone-volume-capsule');
        this.fill = this.container.querySelector('.phone-volume-fill');
        this.display = this.container.querySelector('.phone-volume-badge-val');
        this.icon = this.container.querySelector('.phone-volume-icon');

        this.min = parseFloat(options.min ?? this.container.dataset.min ?? 0);
        this.max = parseFloat(options.max ?? this.container.dataset.max ?? 100);
        this.step = parseFloat(options.step ?? this.container.dataset.step ?? 1);
        this.unit = options.unit ?? this.container.dataset.unit ?? '';
        this.isHorizontal = this.capsule.classList.contains('horizontal');

        const rawStr = this.input ? String(this.input.value).replace(',', '.').trim() : String(options.value ?? this.min);
        const initialVal = parseFloat(rawStr);
        this.value = isNaN(initialVal) ? this.min : initialVal;

        this.isDragging = false;
        this.audioCtx = null;

        this.initEvents();
        this.updateUI(this.value, false);
    }

    playHapticBeep(frequency = 440) {
        try {
            if (!this.audioCtx) {
                const AudioContext = window.AudioContext || window.webkitAudioContext;
                if (AudioContext) this.audioCtx = new AudioContext();
            }
            if (this.audioCtx && this.audioCtx.state === 'suspended') {
                this.audioCtx.resume();
            }
            if (this.audioCtx) {
                const osc = this.audioCtx.createOscillator();
                const gain = this.audioCtx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime(frequency, this.audioCtx.currentTime);
                gain.gain.setValueAtTime(0.04, this.audioCtx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.0001, this.audioCtx.currentTime + 0.05);
                osc.connect(gain);
                gain.connect(this.audioCtx.destination);
                osc.start();
                osc.stop(this.audioCtx.currentTime + 0.05);
            }
        } catch (e) {
            // Audio context not allowed or not supported
        }
    }

    initEvents() {
        const onStart = (e) => {
            this.isDragging = true;
            this.capsule.classList.add('active-dragging');
            this.handlePointer(e);
            window.addEventListener('mousemove', onMove);
            window.addEventListener('mouseup', onEnd);
            window.addEventListener('touchmove', onMove, { passive: false });
            window.addEventListener('touchend', onEnd);
        };

        const onMove = (e) => {
            if (!this.isDragging) return;
            if (e.cancelable) e.preventDefault();
            this.handlePointer(e);
        };

        const onEnd = () => {
            if (!this.isDragging) return;
            this.isDragging = false;
            this.capsule.classList.remove('active-dragging');
            window.removeEventListener('mousemove', onMove);
            window.removeEventListener('mouseup', onEnd);
            window.removeEventListener('touchmove', onMove);
            window.removeEventListener('touchend', onEnd);
        };

        this.capsule.addEventListener('mousedown', onStart);
        this.capsule.addEventListener('touchstart', onStart, { passive: false });

        // Wheel event support
        this.capsule.addEventListener('wheel', (e) => {
            e.preventDefault();
            const delta = e.deltaY < 0 ? this.step : -this.step;
            this.setValue(this.value + delta);
        }, { passive: false });

        // Preset buttons
        const presetBtns = this.container.querySelectorAll('.volume-preset-btn');
        presetBtns.forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const val = parseFloat(btn.dataset.val);
                this.setValue(val);
                this.playHapticBeep(620);
            });
        });

        // Step buttons (+ / -)
        const decBtn = this.container.querySelector('.volume-step-dec');
        if (decBtn) {
            decBtn.addEventListener('click', (e) => {
                e.preventDefault();
                this.setValue(this.value - this.step);
                this.playHapticBeep(380);
            });
        }
        const incBtn = this.container.querySelector('.volume-step-inc');
        if (incBtn) {
            incBtn.addEventListener('click', (e) => {
                e.preventDefault();
                this.setValue(this.value + this.step);
                this.playHapticBeep(540);
            });
        }
    }

    handlePointer(e) {
        const rect = this.capsule.getBoundingClientRect();
        const clientX = e.touches ? e.touches[0].clientX : e.clientX;
        const clientY = e.touches ? e.touches[0].clientY : e.clientY;

        let ratio = 0;
        if (this.isHorizontal) {
            ratio = (clientX - rect.left) / rect.width;
        } else {
            // Vertical: bottom is 0%, top is 100%
            ratio = (rect.bottom - clientY) / rect.height;
        }

        ratio = Math.max(0, Math.min(1, ratio));
        let rawVal = this.min + ratio * (this.max - this.min);
        
        // Round to nearest step
        let steppedVal = Math.round((rawVal - this.min) / this.step) * this.step + this.min;
        steppedVal = Math.max(this.min, Math.min(this.max, steppedVal));
        
        // Step precision handling
        if (this.step < 1) {
            steppedVal = parseFloat(steppedVal.toFixed(2));
        } else {
            steppedVal = Math.round(steppedVal);
        }

        if (steppedVal !== this.value) {
            this.playHapticBeep(440 + ratio * 300);
            this.setValue(steppedVal);
        }
    }

    setValue(newVal) {
        newVal = Math.max(this.min, Math.min(this.max, newVal));
        if (this.step < 1) {
            newVal = parseFloat(newVal.toFixed(2));
        } else {
            newVal = Math.round(newVal);
        }
        this.value = newVal;
        this.updateUI(newVal, true);
    }

    updateUI(val, triggerChange = true) {
        if (this.input) {
            this.input.value = val;
            if (triggerChange) {
                this.input.dispatchEvent(new Event('change', { bubbles: true }));
                this.input.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }

        const ratio = (val - this.min) / (this.max - this.min);
        const percent = Math.round(ratio * 100);

        if (this.isHorizontal) {
            this.fill.style.width = `${percent}%`;
        } else {
            this.fill.style.height = `${percent}%`;
        }

        if (this.display) {
            this.display.textContent = `${val} ${this.unit}`.trim();
        }

        // Dynamic icon styling
        if (this.icon) {
            if (this.unit === 'm') {
                if (val <= 80) this.icon.className = 'bi bi-geo-alt-fill phone-volume-icon text-success';
                else if (val <= 250) this.icon.className = 'bi bi-radar phone-volume-icon text-info';
                else this.icon.className = 'bi bi-broadcast-pin phone-volume-icon text-warning';
            } else if (this.unit === '%') {
                if (val === 0) this.icon.className = 'bi bi-shield-check phone-volume-icon text-success';
                else if (val <= 3) this.icon.className = 'bi bi-exclamation-triangle-fill phone-volume-icon text-warning';
                else this.icon.className = 'bi bi-percent phone-volume-icon text-danger';
            }
        }

        // Update active preset button highlight
        const presetBtns = this.container.querySelectorAll('.volume-preset-btn');
        presetBtns.forEach(btn => {
            const pVal = parseFloat(btn.dataset.val);
            if (Math.abs(pVal - val) < 0.01) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });
    }
}

// Auto-initialize all phone volume slider widgets on page
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.phone-volume-widget').forEach(el => {
        if (!el._volumeSlider) {
            el._volumeSlider = new PhoneVolumeSlider(el.id);
        }
    });
});
