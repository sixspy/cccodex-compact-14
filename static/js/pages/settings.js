import { api } from '../api.js';

export async function renderSettings(view, setMsg) {
  const j = await api('/api/settings');
  const threshold = (j.items || []).find(s => s.key === 'compact_threshold_kb');
  const gates = await api('/api/gate/checks');
  view.innerHTML = `
    <section class="card">
      <h2>台参数</h2>
      <p class="muted">压实阈值与对账模式；保存后立即写库，下次启动合并按现行阈值闸检。</p>
      ${threshold ? `<p class="hint">现行碎件阈值：<strong>${threshold.value}KB</strong>；低于该值的碎件在合并前闸检被拒。</p>` : ''}
      <div class="wall">
        ${(j.items || []).map(s => `<article class="card">
          <h3>${s.label}</h3>
          <p class="muted">${s.key}</p>
          <input data-key="${s.key}" value="${s.value}" />
          <button data-save="${s.key}" class="primary" type="button">保存</button>
        </article>`).join('')}
      </div>
    </section>
    <section class="card" style="margin-top:1rem">
      <h2>闸检账册</h2>
      <p class="muted">每次启动合并的单次落盘凭证；页面与库表同源，可抽核。</p>
      ${(gates.items || []).map(g => `<article class="card" data-gate="${g.id}">
        <h3>闸检 #${g.id} · ${g.plan_code || ('计划#'+g.plan_id)}</h3>
        ${g.status === '通过' ? '<span class="chip chip-ok">通过</span>' : '<span class="chip chip-bad">拒绝</span>'}
        <p class="muted">${g.created_at} · 阈值 ${g.threshold_kb}KB · 核验 ${g.checked_count} 件 · 被拒 ${g.rejected_count} 件</p>
        <p>${g.message}</p>
        ${(g.rejected_detail || []).length ? `<ul class="reject-list">
          ${g.rejected_detail.map(d => `<li><code>${d.path}</code> · ${d.size_bytes} 字节
            <span class="muted">— ${d.reason}</span></li>`).join('')}
        </ul>` : ''}
      </article>`).join('') || '<p class="muted">暂无闸检凭证</p>'}
    </section>`;
  view.querySelectorAll('[data-save]').forEach(btn => {
    btn.onclick = async () => {
      try {
        const key = btn.dataset.save;
        const input = view.querySelector('input[data-key="'+key+'"]');
        await api('/api/settings', { method:'POST', body: JSON.stringify({ key, value: input.value }) });
        setMsg('已保存 ' + key);
        await renderSettings(view, setMsg);
      } catch (e) { setMsg(e.message, true); }
    };
  });
}
