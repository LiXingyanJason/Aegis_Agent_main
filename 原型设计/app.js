const routes = [
  ['index.html', '◈', '原型总览'],
  ['task-console.html', '◌', '任务对话'],
  ['research.html', '⌕', '邮件管理'],
  ['official-research.html', '▤', '官方资料查询'],
  ['confirmation.html', '✓', '操作确认'],
  ['sandbox.html', '▣', '代码沙箱'],
  ['memory.html', '◇', '长期记忆'],
  ['connections-audit.html', '◫', '连接与审计'],
  ['task-states.html', '!', '异常与恢复'],
];

if (decodeURIComponent(location.pathname).split('/').pop() === 'confirmation.html') {
  document.body.classList.add('approval-page');
}

function renderNavigation() {
  const side = document.querySelector('.side');
  if (!side) return;
  const current = decodeURIComponent(location.pathname).split('/').pop() || 'index.html';
  const links = routes.map(([path, icon, label]) =>
    `<a class="nav ${path === current ? 'active' : ''}" href="${path}">${icon} ${label}</a>`,
  ).join('');
  side.innerHTML = `<div class="brand"><span class="mark">⌾</span>Aegis PA</div><p class="label">原型页面</p>${links}<div class="who"><b>王晓晨</b>个人工作空间</div>`;
}

function toast(message) {
  const target = document.querySelector('.toast');
  if (!target) return;
  target.textContent = message;
  target.classList.add('show');
  setTimeout(() => target.classList.remove('show'), 2600);
}

renderNavigation();
document.querySelectorAll('.source-summary a[href="research.html"]').forEach(link => {
  link.href = 'official-research.html';
});
document.querySelectorAll('[data-toast]').forEach(button => {
  button.onclick = () => toast(button.dataset.toast);
});
document.querySelectorAll('[data-open]').forEach(button => {
  button.onclick = () => document.getElementById(button.dataset.open)?.classList.add('show');
});
document.querySelectorAll('[data-close]').forEach(button => {
  button.onclick = () => button.closest('.modalback')?.classList.remove('show');
});
document.querySelectorAll('.modalback').forEach(backdrop => {
  backdrop.onclick = event => { if (event.target === backdrop) backdrop.classList.remove('show'); };
});
document.querySelectorAll('a[href="#"]').forEach(link => {
  link.onclick = event => event.preventDefault();
});

const mailTabs = document.querySelectorAll('[data-mail-tab]');
const mailPanels = document.querySelectorAll('[data-mail-panel]');
if (mailTabs.length) {
  const selectMailPanel = name => {
    mailTabs.forEach(tab => {
      const selected = tab.dataset.mailTab === name;
      tab.classList.toggle('active', selected);
      tab.setAttribute('aria-selected', String(selected));
    });
    mailPanels.forEach(panel => { panel.hidden = panel.dataset.mailPanel !== name; });
  };
  const initialTab = location.hash.replace('#', '');
  selectMailPanel(['received', 'drafts', 'sent'].includes(initialTab) ? initialTab : 'received');
  mailTabs.forEach(tab => {
    tab.onclick = () => {
      const name = tab.dataset.mailTab;
      location.hash = name;
      selectMailPanel(name);
    };
  });
}

document.querySelectorAll('[data-email-generate]').forEach(button => {
  button.onclick = () => {
    const row = button.closest('tr');
    const summary = row.querySelector('[data-email-summary-text]');
    const action = row.querySelector('[data-email-action-text]');
    const todo = row.querySelector('[data-email-todo]');
    button.disabled = true;
    button.textContent = '生成中...';
    toast('原型模式：正在调用 LLM 生成邮件摘要。');
    setTimeout(() => {
      summary.textContent = button.dataset.summary;
      action.innerHTML = `<b>${button.dataset.action}</b><br><span class="badge ${button.dataset.due === '暂无明确截止时间' ? 'neutral' : 'write'}">${button.dataset.due}</span>`;
      todo.disabled = false;
      button.textContent = '已生成';
      toast('原型模式：已生成关键信息与待办。');
    }, 450);
  };
});

document.querySelectorAll('[data-email-todo]').forEach(button => {
  button.onclick = () => toast('原型模式：已将当前邮件的待办加入草稿。');
});
document.querySelectorAll('[data-email-reply]').forEach(button => {
  button.onclick = () => toast('原型模式：已打开当前邮件的回复草稿，不会直接发送。');
});

document.querySelectorAll('[data-approval-item]').forEach(item => {
  const check = item.querySelector('[data-approval-check]');
  const status = item.querySelector('[data-approval-status]');
  const execute = item.querySelector('[data-approval-execute]');
  const reject = item.querySelector('[data-approval-reject]');
  const updateSummary = () => {
    const approved = document.querySelectorAll('[data-approval-status].success').length;
    const summary = document.querySelector('[data-approval-summary]');
    if (summary) summary.textContent = `${approved} 项已批准`;
  };
  if (execute) execute.onclick = () => {
    if (!check.checked) { alert('请先核对并确认当前这一项操作。'); return; }
    status.className = 'badge success';
    status.textContent = '已批准执行';
    execute.disabled = true;
    reject.disabled = true;
    if (item.hasAttribute('data-mail-send-approval')) {
      item.querySelector('[data-sent-link]')?.removeAttribute('hidden');
    }
    toast('原型模式：已批准当前操作，正在独立执行。');
    updateSummary();
  };
  if (reject) reject.onclick = () => {
    status.className = 'badge blocked';
    status.textContent = '已拒绝';
    execute.disabled = true;
    reject.disabled = true;
    toast('原型模式：已拒绝当前操作，其他任务不受影响。');
    updateSummary();
  };
});

const search = document.querySelector('[data-search]');
if (search) {
  search.oninput = () => document.querySelectorAll('[data-item]').forEach(item => {
    item.hidden = !item.textContent.toLowerCase().includes(search.value.toLowerCase());
  });
}

const form = document.querySelector('.compose');
if (form) {
  form.onsubmit = event => {
    event.preventDefault();
    const input = form.querySelector('input');
    if (input.value.trim()) {
      toast('原型模式：消息已加入待处理队列。');
      input.value = '';
    }
  };
}
