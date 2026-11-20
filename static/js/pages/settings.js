import { api } from '../api.js';

export async function renderSettings(view, setMsg) {
  const j = await api('/api/settings');
  view.innerHTML = `
    <section class="card">
      <h2>台参数</h2>
      <p class="muted">压实阈值与对账模式；保存后立即写库。</p>
      <div class="wall">
        ${(j.items || []).map(s => `<article class="card">
          <h3>${s.label}</h3>
          <p class="muted">${s.key}</p>
          <input data-key="${s.key}" value="${s.value}" />
          <button data-save="${s.key}" class="primary" type="button">保存</button>
        </article>`).join('')}
      </div>
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
