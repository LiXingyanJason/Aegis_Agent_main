import http from './http'

export async function listTodos(status) {
  const response = await http.get('/todos', {
    params: status ? { status } : {},
  })
  return response.data.data
}

export async function getTodo(todoId) {
  const response = await http.get('/todos/' + todoId)
  return response.data.data
}

export async function updateTodo(todoId, payload) {
  const response = await http.patch('/todos/' + todoId, payload)
  return response.data.data
}
