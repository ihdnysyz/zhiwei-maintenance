import type { ObjectDirective } from 'vue'

type Close = () => void
const sheetGestures = new WeakMap<HTMLDialogElement, { close: Close; dispose: () => void }>()

export const sheetDrag: ObjectDirective<HTMLDialogElement, Close> = {
  mounted(sheet, binding) {
    const header = sheet.querySelector<HTMLElement>('.sheet-head')
    if (!header) return

    const mobile = window.matchMedia('(max-width: 650px)')
    let drag: { id: number; x: number; y: number; distance: number } | null = null
    const reset = () => {
      drag = null
      sheet.classList.remove('sheet-dragging')
      sheet.style.removeProperty('--sheet-drag')
    }
    const start = (event: PointerEvent) => {
      if (!mobile.matches || drag || !event.isPrimary || event.pointerType === 'mouse' ||
          !(event.target instanceof Element) || event.target.closest('button, a, input, select, textarea')) return
      drag = { id: event.pointerId, x: event.clientX, y: event.clientY, distance: 0 }
      header.setPointerCapture(event.pointerId)
    }
    const move = (event: PointerEvent) => {
      if (!drag || event.pointerId !== drag.id) return
      const distance = event.clientY - drag.y
      if (!drag.distance && (distance < 4 || Math.abs(event.clientX - drag.x) > distance)) return
      drag.distance = Math.max(distance, 0)
      sheet.classList.add('sheet-dragging')
      sheet.style.setProperty('--sheet-drag', `${drag.distance}px`)
    }
    const finish = (event: PointerEvent) => {
      if (event.pointerId !== drag?.id) return
      if (event.type === 'pointerup' && drag.distance >= 88) {
        drag = null
        sheet.classList.remove('sheet-dragging')
        sheetGestures.get(sheet)?.close()
      } else reset()
    }
    header.addEventListener('pointerdown', start)
    header.addEventListener('pointermove', move)
    header.addEventListener('pointerup', finish)
    header.addEventListener('pointercancel', finish)
    header.addEventListener('lostpointercapture', finish)
    const dispose = () => {
      header.removeEventListener('pointerdown', start)
      header.removeEventListener('pointermove', move)
      header.removeEventListener('pointerup', finish)
      header.removeEventListener('pointercancel', finish)
      header.removeEventListener('lostpointercapture', finish)
    }
    sheetGestures.set(sheet, { close: binding.value, dispose })
  },
  updated(sheet, binding) {
    const gesture = sheetGestures.get(sheet)
    if (gesture) gesture.close = binding.value
  },
  beforeUnmount(sheet) {
    sheetGestures.get(sheet)?.dispose()
    sheetGestures.delete(sheet)
  },
}
