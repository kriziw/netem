// Clock times in the format the operator chose in Settings: 24-hour (the default) or 12-hour with AM/PM.
window.NetEmTime = (() => {
  const FORMATS = ['24h', '12h'];
  let format = '24h';
  if (typeof document !== 'undefined' && FORMATS.includes(document.body?.dataset?.timeFormat)) format = document.body.dataset.timeFormat;
  const pad = value => String(value).padStart(2, '0');
  // Numbers are Unix seconds, as the simulator and telemetry report them; Dates pass through.
  const toDate = value => {
    // Duck-typed: a Date from another realm (an iframe or a test sandbox) is still a Date.
    if (value && typeof value.getTime === 'function') return value;
    if (value == null || value === '') return new Date(NaN);
    return new Date(Number(value) * 1000);
  };
  // Hours, minutes and optionally seconds of a moment, in the selected format.
  function clock(value, {seconds = true} = {}) {
    const date = toDate(value);
    if (Number.isNaN(date.getTime())) return '—';
    const tail = (seconds ? ':' + pad(date.getSeconds()) : '');
    if (format === '12h') {
      const hours = date.getHours();
      return `${hours % 12 || 12}:${pad(date.getMinutes())}${tail} ${hours < 12 ? 'AM' : 'PM'}`;
    }
    return `${pad(date.getHours())}:${pad(date.getMinutes())}${tail}`;
  }
  // A date followed by its clock time, for session and inventory records.
  function stamp(value, {seconds = true} = {}) {
    const date = toDate(value);
    if (Number.isNaN(date.getTime())) return '—';
    return `${date.toLocaleDateString(undefined, {dateStyle: 'medium'})} ${clock(date, {seconds})}`;
  }
  function setFormat(value) {
    format = FORMATS.includes(value) ? value : '24h';
  }
  return {clock, stamp, setFormat, format: () => format};
})();
