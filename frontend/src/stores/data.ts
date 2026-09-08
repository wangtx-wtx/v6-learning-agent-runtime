/**
 * 全局数据 store：缓存课程 / 章节 / 课时，便于跨页面使用。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ChaptersApi, CoursesApi, ExamsApi, LessonsApi } from '../api/endpoints'
import type { Chapter, Course, ExamEvent, Lesson } from '../api/endpoints'

export const useDataStore = defineStore('data', () => {
  const courses = ref<Course[]>([])
  const chapters = ref<Chapter[]>([])
  const lessons = ref<Lesson[]>([])
  const exams = ref<ExamEvent[]>([])

  const loaded = ref({
    courses: false,
    chapters: false,
    lessons: false,
    exams: false,
  })

  async function ensureCourses(force = false) {
    if (!force && loaded.value.courses) return courses.value
    courses.value = await CoursesApi.list()
    loaded.value.courses = true
    return courses.value
  }

  async function ensureChapters(force = false) {
    if (!force && loaded.value.chapters) return chapters.value
    chapters.value = await ChaptersApi.list()
    loaded.value.chapters = true
    return chapters.value
  }

  async function ensureLessons(force = false) {
    if (!force && loaded.value.lessons) return lessons.value
    lessons.value = await LessonsApi.list()
    loaded.value.lessons = true
    return lessons.value
  }

  async function ensureExams(force = false) {
    if (!force && loaded.value.exams) return exams.value
    exams.value = await ExamsApi.list()
    loaded.value.exams = true
    return exams.value
  }

  function courseName(id?: number | null) {
    if (!id) return '未指定课程'
    return courses.value.find(c => c.id === id)?.name || `课程 #${id}`
  }

  function chapterTitle(id?: number | null) {
    if (!id) return '未指定章节'
    const ch = chapters.value.find(c => c.id === id)
    if (!ch) return `章节 #${id}`
    const no = ch.chapter_no ? `第${ch.chapter_no}章 ` : ''
    return `${no}${ch.title}`
  }

  function lessonTitle(id?: number | null) {
    if (!id) return '未指定课时'
    const ls = lessons.value.find(l => l.id === id)
    if (!ls) return `课时 #${id}`
    const no = ls.lesson_no ? `${ls.lesson_no} ` : ''
    return `${no}${ls.title || ''}`.trim() || `课时 #${id}`
  }

  return {
    courses,
    chapters,
    lessons,
    exams,
    loaded,
    ensureCourses,
    ensureChapters,
    ensureLessons,
    ensureExams,
    courseName,
    chapterTitle,
    lessonTitle,
  }
})
