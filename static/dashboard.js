(async function () {
    'use strict';

    const MODEL_COLORS = {
        Haiku:  { bg: 'rgba(63, 185, 80, 0.5)',  border: '#3fb950' },
        Sonnet: { bg: 'rgba(88, 166, 255, 0.5)', border: '#58a6ff' },
        Opus:   { bg: 'rgba(188, 140, 255, 0.5)', border: '#bc8cff' },
    };
    const FALLBACK_COLOR = { bg: 'rgba(139, 148, 158, 0.5)', border: '#8b949e' };

    const RANGE_SEC = {
        '1h': 3600,
        '6h': 21600,
        '24h': 86400,
        '7d': 604800,
        '30d': 2592000,
        'all': Infinity,
    };

    let allEntries = [];
    let currentRange = '24h';
    let chart = null;

    // --- Load data ---
    try {
        const resp = await fetch('/api/costs');
        allEntries = await resp.json();
    } catch (e) {
        console.error('Failed to load costs:', e);
    }

    document.getElementById('entry-count').textContent = `${allEntries.length} entries`;

    // --- Filter bar ---
    document.querySelectorAll('#filter-bar .filter-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('#filter-bar .filter-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentRange = btn.dataset.range;
            render();
        });
    });

    // --- Filtering ---
    function getFiltered() {
        if (currentRange === 'all') return allEntries;
        const cutoff = Date.now() / 1000 - RANGE_SEC[currentRange];
        return allEntries.filter(e => e.timestamp >= cutoff);
    }

    // --- Render all ---
    function render() {
        const entries = getFiltered();
        renderSummary(entries);
        renderChart(entries);
        renderModelTable(entries);
        renderRecentTable(entries);
    }

    // --- Summary Cards ---
    function renderSummary(entries) {
        const totalUsd = entries.reduce((s, e) => s + (e.cost_usd || 0), 0);
        const totalKrw = entries.reduce((s, e) => s + (e.cost_krw || 0), 0);
        const totalRequests = entries.length;
        const totalTokens = entries.reduce((s, e) =>
            s + (e.input_tokens || 0) + (e.output_tokens || 0), 0);

        document.getElementById('summary-cards').innerHTML = `
            <div class="summary-card">
                <div class="value">$${totalUsd.toFixed(4)}</div>
                <div class="label">Total USD</div>
            </div>
            <div class="summary-card">
                <div class="value">${Math.round(totalKrw).toLocaleString()}원</div>
                <div class="label">Total KRW</div>
            </div>
            <div class="summary-card">
                <div class="value">${totalRequests}</div>
                <div class="label">Requests</div>
            </div>
            <div class="summary-card">
                <div class="value">${fmtTokens(totalTokens)}</div>
                <div class="label">Total Tokens</div>
            </div>
        `;
    }

    // --- Chart ---
    function renderChart(entries) {
        if (entries.length === 0) {
            if (chart) { chart.destroy(); chart = null; }
            return;
        }

        // Adaptive bucket size
        const rangeSec = RANGE_SEC[currentRange];
        let bucketSec, timeFmt;
        if (rangeSec <= 3600) {
            bucketSec = 300; timeFmt = 'HH:mm';
        } else if (rangeSec <= 86400) {
            bucketSec = 3600; timeFmt = 'HH:mm';
        } else {
            bucketSec = 86400; timeFmt = 'MM/DD';
        }

        // Group by bucket + model
        const buckets = new Map();
        const models = new Set();

        for (const e of entries) {
            const key = Math.floor(e.timestamp / bucketSec) * bucketSec;
            const model = e.model || 'unknown';
            models.add(model);
            if (!buckets.has(key)) buckets.set(key, {});
            buckets.get(key)[model] = (buckets.get(key)[model] || 0) + (e.cost_usd || 0);
        }

        const sortedKeys = [...buckets.keys()].sort((a, b) => a - b);
        const labels = sortedKeys.map(k => {
            const d = new Date(k * 1000);
            if (timeFmt === 'HH:mm') {
                return d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Seoul' });
            }
            return d.toLocaleDateString('ko-KR', { month: '2-digit', day: '2-digit', timeZone: 'Asia/Seoul' });
        });

        const datasets = [...models].sort().map(model => {
            const colors = MODEL_COLORS[model] || FALLBACK_COLOR;
            return {
                label: model,
                data: sortedKeys.map(k => +(buckets.get(k)?.[model] || 0).toFixed(6)),
                backgroundColor: colors.bg,
                borderColor: colors.border,
                borderWidth: 1,
            };
        });

        const ctx = document.getElementById('cost-chart').getContext('2d');
        if (chart) chart.destroy();

        chart = new Chart(ctx, {
            type: 'bar',
            data: { labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { labels: { color: '#8b949e', font: { size: 11 } } },
                    tooltip: {
                        callbacks: {
                            label: ctx => `${ctx.dataset.label}: $${ctx.parsed.y.toFixed(4)}`,
                        },
                    },
                },
                scales: {
                    x: {
                        stacked: true,
                        ticks: { color: '#8b949e', font: { size: 10 }, maxRotation: 45 },
                        grid: { color: '#21262d' },
                    },
                    y: {
                        stacked: true,
                        ticks: {
                            color: '#8b949e',
                            font: { size: 10 },
                            callback: v => `$${v.toFixed(4)}`,
                        },
                        grid: { color: '#21262d' },
                        title: { display: true, text: 'Cost (USD)', color: '#8b949e' },
                    },
                },
            },
        });
    }

    // --- Model Table ---
    function renderModelTable(entries) {
        const models = {};
        for (const e of entries) {
            const m = e.model || 'unknown';
            if (!models[m]) {
                models[m] = { requests: 0, inTok: 0, outTok: 0, usd: 0, krw: 0, rateSum: 0 };
            }
            const r = models[m];
            r.requests++;
            r.inTok += (e.input_tokens || 0) + (e.cache_read_tokens || 0) + (e.cache_creation_tokens || 0);
            r.outTok += (e.output_tokens || 0);
            r.usd += (e.cost_usd || 0);
            r.krw += (e.cost_krw || 0);
            r.rateSum += (e.exchange_rate || 0);
        }

        const tbody = document.querySelector('#model-table tbody');
        tbody.innerHTML = '';

        let tReq = 0, tIn = 0, tOut = 0, tUsd = 0, tKrw = 0;

        for (const [name, r] of Object.entries(models).sort()) {
            const avgRate = r.requests > 0 ? Math.round(r.rateSum / r.requests).toLocaleString() : '-';
            tbody.innerHTML += `<tr>
                <td>${name}</td>
                <td>${r.requests}</td>
                <td>${fmtTokens(r.inTok)}</td>
                <td>${fmtTokens(r.outTok)}</td>
                <td>$${r.usd.toFixed(4)}</td>
                <td>${Math.round(r.krw).toLocaleString()}원</td>
                <td>${avgRate}</td>
            </tr>`;
            tReq += r.requests; tIn += r.inTok; tOut += r.outTok; tUsd += r.usd; tKrw += r.krw;
        }

        tbody.innerHTML += `<tr class="total-row">
            <td>Total</td>
            <td>${tReq}</td>
            <td>${fmtTokens(tIn)}</td>
            <td>${fmtTokens(tOut)}</td>
            <td>$${tUsd.toFixed(4)}</td>
            <td>${Math.round(tKrw).toLocaleString()}원</td>
            <td>-</td>
        </tr>`;
    }

    // --- Recent Requests Table ---
    function renderRecentTable(entries) {
        const tbody = document.querySelector('#recent-table tbody');
        tbody.innerHTML = '';

        const recent = [...entries].reverse().slice(0, 50);
        for (const e of recent) {
            const t = new Date(e.timestamp * 1000);
            const timeStr = t.toLocaleString('ko-KR', {
                month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit',
                timeZone: 'Asia/Seoul',
            });
            const tokens = (e.input_tokens || 0) + (e.output_tokens || 0);
            const dur = e.duration_ms ? `${(e.duration_ms / 1000).toFixed(1)}s` : '-';
            const rate = e.exchange_rate ? Math.round(e.exchange_rate).toLocaleString() : '-';

            tbody.innerHTML += `<tr>
                <td>${timeStr}</td>
                <td>${e.model || '?'}</td>
                <td>${fmtTokens(tokens)}</td>
                <td>$${(e.cost_usd || 0).toFixed(4)}</td>
                <td>${Math.round(e.cost_krw || 0).toLocaleString()}원</td>
                <td>${rate}</td>
                <td>${dur}</td>
            </tr>`;
        }
    }

    // --- Utility ---
    function fmtTokens(n) {
        if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
        if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
        return String(n);
    }

    // --- Go ---
    render();
})();
