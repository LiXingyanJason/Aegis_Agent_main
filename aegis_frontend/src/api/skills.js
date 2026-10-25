import http from './http'

const skillPath = (skillId) => '/skills/' + encodeURIComponent(skillId)

export async function listSkillTemplates() {
  return (await http.get('/skills/templates')).data.data
}

export async function listSkills() {
  // 服务端最多保留 20 条未删除个人 Skill；一次读取全部，避免 UUID 游标被误当时间排序。
  return (await http.get('/skills', { params: { limit: 100 } })).data.data
}

export async function createSkill(payload) {
  return (await http.post('/skills', payload)).data.data
}

export async function getSkill(skillId) {
  return (await http.get(skillPath(skillId))).data.data
}

export async function updateSkill(skillId, payload) {
  return (await http.put(skillPath(skillId), payload)).data.data
}

export async function publishSkill(skillId, expectedRevision) {
  return (await http.post(skillPath(skillId) + '/publish', { expected_revision: expectedRevision })).data.data
}

export async function setSkillEnabled(skillId, expectedRevision, enabled) {
  return (await http.patch(skillPath(skillId) + '/enabled', { expected_revision: expectedRevision, enabled })).data.data
}

export async function deleteSkill(skillId, expectedRevision) {
  return (await http.delete(skillPath(skillId), { data: { expected_revision: expectedRevision } })).data.data
}

export async function listSkillVersions(skillId, before) {
  return (await http.get(skillPath(skillId) + '/versions', {
    params: { limit: 20, ...(before ? { before } : {}) },
  })).data.data
}

export async function getSkillVersion(skillId, version) {
  return (await http.get(skillPath(skillId) + '/versions/' + encodeURIComponent(version))).data.data
}

export async function getRunSkillUsages(runId, cursor) {
  return (await http.get('/skills/runs/' + encodeURIComponent(runId) + '/usages', {
    params: { limit: 50, ...(cursor ? { cursor } : {}) },
  })).data.data
}
