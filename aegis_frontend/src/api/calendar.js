import http from './http'

export async function listCalendars() {
  const response = await http.get('/calendar/calendars')
  return response.data.data
}

export async function listCalendarEvents({ connectionId, calendarId = 'primary', startAt, endAt, timezone = 'Asia/Shanghai' }) {
  const response = await http.get('/calendar/events', {
    params: {
      connection_id: connectionId,
      calendar_id: calendarId,
      start_at: startAt,
      end_at: endAt,
      timezone,
    },
  })
  return response.data.data
}

export async function getCalendarEventDetail({ connectionId, calendarId = 'primary', externalEventId, timezone = 'Asia/Shanghai' }) {
  const response = await http.get('/calendar/events/detail', {
    params: {
      connection_id: connectionId,
      calendar_id: calendarId,
      external_event_id: externalEventId,
      timezone,
    },
  })
  return response.data.data
}

export async function listCalendarDrafts({ startAt, endAt, connectionId, approvalStatus = 'pending', timezone = 'Asia/Shanghai' }) {
  const response = await http.get('/calendar/drafts', {
    params: {
      start_at: startAt,
      end_at: endAt,
      ...(connectionId ? { connection_id: connectionId } : {}),
      approval_status: approvalStatus,
      timezone,
    },
  })
  return response.data.data
}

export async function getCalendarDraftDetail(draftId) {
  const response = await http.get('/calendar/drafts/' + encodeURIComponent(draftId))
  return response.data.data
}
