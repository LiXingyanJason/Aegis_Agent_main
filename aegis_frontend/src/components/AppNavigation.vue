<script setup>
defineProps({ active: { type: String, required: true } })

const items = [
  { name: 'today', label: '今日工作台', path: 'M3 3h8v8H3z M14 3h7v5h-7z M14 11h7v10h-7z M3 14h8v7H3z' },
  { name: 'tasks', label: '任务对话', path: 'M4 4h16v13H9l-5 4v-4H4z M8 9h8 M8 12.5h5' },
  { name: 'calendar', label: '日历', path: 'M4 5h16v16H4z M4 9h16 M8 3v4 M16 3v4 M8 13h3v3H8z' },
  { name: 'mail-management', label: '邮件管理', path: 'M3 6h18v13H3z M3 7l9 7 9-7' },
  { name: 'todo-plans', label: '待办计划', path: 'M5 3h14v18H5z M8 8l1.5 1.5L12 7 M14 8h3 M8 15l1.5 1.5L12 14 M14 15h3' },
  { name: 'confirmations', label: '操作确认', path: 'M12 2l8 3v6c0 5-3 8-8 11-5-3-8-6-8-11V5z M8 11l2.5 2.5L16 8' },
  { name: 'knowledge', label: '知识资料', path: 'M12 5C9 3.5 6 3.5 3 4v15c3-.5 6-.5 9 1 3-1.5 6-1.5 9-1V4c-3-.5-6-.5-9 1z M12 5v15' },
  { name: 'skills', label: 'Skill 管理', path: 'M9 3h6l1 3 3 1-1 3 3 2-3 2 1 3-3 1-1 3H9l-1-3-3-1 1-3-3-2 3-2-1-3 3-1z M9 12h6 M12 9v6' },
  { name: 'memories', label: '长期记忆', path: 'M12 4c-2-2-5-1-6 1-2 0-3 2-2 4-2 1-2 4 0 5-1 3 1 5 4 5 1 2 4 2 4 0 0 2 3 2 4 0 3 0 5-2 4-5 2-1 2-4 0-5 1-2 0-4-2-4-1-2-4-3-6-1z M12 4v15 M8 9l4 2 M16 9l-4 2 M8 15l4-2 M16 15l-4-2' },
  { name: 'connections', label: '连接与审计（待接入）', path: 'M4 8h5v8H4z M9 12h5 M14 9h6v6h-6z M16 9V5 M18 9V5 M17 15v4' },
]
</script>

<template>
  <p class="nav-label app-nav-label">工作空间</p>
  <template v-for="item in items" :key="item.name">
    <span v-if="item.name === 'connections'" class="nav disabled app-nav-link" aria-disabled="true">
      <svg class="app-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="item.path" /></svg>
      <span>{{ item.label }}</span>
    </span>
    <a v-else-if="item.name === 'tasks' && active === 'tasks'" class="nav active app-nav-link" href="#task-console" aria-current="page">
      <svg class="app-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="item.path" /></svg>
      <span>{{ item.label }}</span>
    </a>
    <router-link v-else class="nav app-nav-link" :class="{ active: active === item.name }" :to="{ name: item.name }" :aria-current="active === item.name ? 'page' : undefined">
      <svg class="app-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="item.path" /></svg>
      <span>{{ item.label }}</span>
    </router-link>
  </template>
</template>

<style scoped>
.app-nav-link { display: flex; align-items: center; gap: 9px; white-space: nowrap; }
.app-nav-icon { width: 17px; height: 17px; flex: none; stroke-width: 1.8; }
.app-nav-link.disabled:hover { background: transparent; }
</style>
