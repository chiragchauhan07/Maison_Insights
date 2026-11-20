/* Grayscale pointer-responsive cell grid for the landing page. */
(function () {
  const shades = ["#ffffff", "#f5f5f5", "#e6e6e6", "#d6d6d6", "#c6c6c6"];
  const cell = 26;
  const reach = 2.6;
  const maxLit = 180;
  const fadeIn = 150;
  const fadeOut = 760;
  const pad = 6;

  class GridPulse {
    constructor(root) {
      this.root = root;
      this.canvas = root.querySelector("canvas");
      this.ctx = this.canvas && this.canvas.getContext("2d");
      this.cells = new Map();
      this.frame = 0;
      this.pending = 0;
      this.timer = 0;
      this.visible = true;
      this.textRects = [];
      this.pointer = null;
      this.motion = matchMedia("(prefers-reduced-motion: reduce)");
      if (!this.ctx || this.motion.matches) return;

      this.resize = new ResizeObserver(() => this.measure());
      this.resize.observe(root);
      this.intersection = new IntersectionObserver(([entry]) => {
        this.visible = entry ? entry.isIntersecting : true;
        if (this.visible) {
          if (this.cells.size) this.wake();
          this.scheduleAmbient(450);
        } else {
          clearTimeout(this.timer);
          this.timer = 0;
        }
      });
      this.intersection.observe(root);
      this.mutations = new MutationObserver(() => this.measureText());
      this.mutations.observe(root.parentElement || document.body, { childList: true, subtree: true, characterData: true });
      this.onPointerMove = (event) => this.trackPointer(event);
      this.onVisibility = () => {
        if (document.hidden) { clearTimeout(this.timer); this.timer = 0; }
        else { if (this.cells.size) this.wake(); this.scheduleAmbient(450); }
      };
      window.addEventListener("pointermove", this.onPointerMove, { passive: true });
      document.addEventListener("visibilitychange", this.onVisibility);
      this.measure();
      this.documentFonts = document.fonts && document.fonts.ready;
      if (this.documentFonts) this.documentFonts.then(() => this.measureText()).catch(() => {});
      this.scheduleAmbient(550);
    }

    measure() {
      const rect = this.root.getBoundingClientRect();
      this.width = this.root.clientWidth;
      this.height = this.root.clientHeight;
      this.cols = Math.max(1, Math.ceil(this.width / cell));
      this.rows = Math.max(1, Math.ceil(this.height / cell));
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.canvas.width = Math.round(this.width * dpr);
      this.canvas.height = Math.round(this.height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.measureText(rect);
      this.wake();
    }

    measureText(rootRect = this.root.getBoundingClientRect()) {
      const scope = this.root.parentElement || document;
      this.textRects = Array.from(scope.querySelectorAll("[data-grid-avoid]")).flatMap((node) => {
        const range = document.createRange();
        range.selectNodeContents(node);
        const lines = Array.from(range.getClientRects()).filter((rect) => rect.width && rect.height);
        const rects = lines.length ? lines : [node.getBoundingClientRect()];
        return rects.map((rect) => ({
          left: rect.left - rootRect.left - pad,
          top: rect.top - rootRect.top - pad,
          right: rect.right - rootRect.left + pad,
          bottom: rect.bottom - rootRect.top + pad,
        }));
      });
    }

    brightness(col, row) {
      const x = col * cell + cell / 2;
      const y = row * cell + cell / 2;
      let nearest = Infinity;
      for (const rect of this.textRects) {
        const dx = Math.max(rect.left - x, 0, x - rect.right);
        const dy = Math.max(rect.top - y, 0, y - rect.bottom);
        nearest = Math.min(nearest, Math.hypot(dx, dy));
        if (!nearest) break;
      }
      return nearest === Infinity ? 1 : .12 + .88 * Math.min(1, nearest / (2.2 * cell));
    }

    wake() {
      if (!this.frame && this.visible && !document.hidden) this.frame = requestAnimationFrame((now) => this.draw(now));
    }

    draw(now) {
      this.frame = 0;
      this.ctx.clearRect(0, 0, this.width, this.height);
      for (const [key, item] of this.cells) {
        let alpha;
        if (now < item.until) alpha = 1 - (1 - Math.min(1, (now - item.born) / fadeIn)) ** 2;
        else {
          const t = (now - item.until) / fadeOut;
          if (t >= 1) { this.cells.delete(key); continue; }
          alpha = 1 - t * t;
        }
        this.ctx.globalAlpha = alpha * item.dim;
        this.ctx.fillStyle = item.shade;
        this.ctx.fillRect(item.col * cell + 1, item.row * cell + 1, cell - 1, cell - 1);
      }
      this.ctx.globalAlpha = 1;
      if (this.cells.size) this.frame = requestAnimationFrame((time) => this.draw(time));
    }

    light(col, row, hold) {
      if (col < 0 || row < 0 || col >= this.cols || row >= this.rows || this.cells.size >= maxLit) return;
      const key = `${col},${row}`;
      const now = performance.now();
      const previous = this.cells.get(key);
      if (previous && now < previous.until) return;
      this.cells.set(key, {
        col, row,
        shade: previous ? previous.shade : shades[Math.floor(Math.random() * shades.length)],
        dim: this.brightness(col, row),
        born: now,
        until: now + hold,
      });
      this.wake();
    }

    paintPointer() {
      this.pending = 0;
      if (!this.pointer || !this.visible || document.hidden) return;
      const cx = Math.floor(this.pointer.x / cell);
      const cy = Math.floor(this.pointer.y / cell);
      const span = Math.ceil(reach);
      for (let dy = -span; dy <= span; dy++) {
        for (let dx = -span; dx <= span; dx++) {
          const distance = Math.hypot(dx, dy);
          if (distance > reach || Math.random() > 1 - distance / (reach + .6)) continue;
          this.light(cx + dx, cy + dy, 280 + Math.random() * 760);
        }
      }
    }

    trackPointer(event) {
      const rect = this.root.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      if (x < 0 || y < 0 || x > rect.width || y > rect.height || !this.visible) {
        this.pointer = null;
        return;
      }
      this.pointer = { x, y };
      if (!this.pending) this.pending = requestAnimationFrame(() => this.paintPointer());
    }

    scheduleAmbient(delay = 1600 + Math.random() * 1800) {
      if (!this.visible || document.hidden || this.timer) return;
      this.timer = window.setTimeout(() => {
        this.timer = 0;
        if (this.visible && !document.hidden) {
          this.light(Math.floor(Math.random() * this.cols), Math.floor(Math.random() * this.rows), 800 + Math.random() * 900);
          this.scheduleAmbient();
        }
      }, delay);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const root = document.querySelector("[data-grid-pulse]");
    if (root) new GridPulse(root);
  });
})();
