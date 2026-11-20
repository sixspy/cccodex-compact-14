import { api } from '../api.js';

function gateChip(status) {
  if (status === '通过') return '<span class="chip chip-ok">闸检通过</span>';
  if (status === '拒绝') return '<span class="chip chip-bad">闸检拒绝</span>';
  return '<span class="chip">未闸检</span>';
}

function rejectedList(detail) {
  if (!detail || !detail.length) return '';
  return `<ul class="reject-list">
    ${detail.map(d => `<li><code>${d.path}</code> · ${d.size_bytes} 字节
      <span class="muted">— ${d.reason}</span></li>`).join('')}
  </ul>`;
}

export async function renderRewrite(view, setMsg) {
  const plans = await api('/api/plans');
  const jobs = await api('/api/rewrite/jobs');
  const options = (plans.items || []).map(p => `<option value="${p.id}">${p.plan_code} · ${p.status}</option>`).join('');
  view.innerHTML = `
    <section class="card">
      <h2>重写合并</h2>
      <p class="muted">对已发布计划启动合并；启动时按现行阈值闸检，低于阈值的碎件被拒且不吞入合并。</p>
      <div class="row">
        <label>计划<select id="pid">${options}</select></label>
        <button class="primary" id="go" type="button">启动合并</button>
      </div>
      <p id="gate-msg" class="muted"></p>
    </section>
    <section class="wall" style="margin-top:1rem">
      ${(jobs.items || []).map(j => `<article class="card" data-job="${j.id}">
        <h3>${j.plan_code || ('计划#'+j.plan_id)}</h3>
        <span class="tag">${j.status}</span>
        ${gateChip(j.gate_status)}
        <p class="muted">${j.message}</p>
        <p>合并字节 ${j.merged_bytes}</p>
        ${j.gate_status ? `<p class="muted">闸检阈值 ${j.gate_threshold_kb}KB · 被拒 ${j.gate_rejected_count} 件</p>` : ''}
        ${rejectedList(j.gate_rejected_detail)}
      </article>`).join('') || '<p class="muted">尚无合并任务</p>'}
    </section>`;
  document.getElementById('go').onclick = async () => {
    try {
      const pid = document.getElementById('pid').value;
      const r = await api('/api/rewrite/plans/' + pid + '/start', { method:'POST', body:'{}' });
      setMsg('合并完成 · ' + r.job.status);
      await renderRewrite(view, setMsg);
    } catch (e) {
      setMsg(e.message, true);
      await renderRewrite(view, setMsg);
      const gate = e.data && e.data.gate;
      if (gate) {
        const gateMsg = document.getElementById('gate-msg');
        if (gateMsg) {
          gateMsg.className = 'err';
          gateMsg.innerHTML = gateChip(gate.status) + ' ' + (gate.message || e.message) +
            rejectedList(gate.rejected_detail);
        }
      }
    }
  };
}
