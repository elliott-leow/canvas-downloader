'use strict';

const form = document.getElementById('download-form');
const submit = document.getElementById('submit-button');
const label = document.getElementById('submit-label');
const progress = document.getElementById('progress');

function updateAuthentication() {
  const method = form.elements.auth_method.value;
  document.getElementById('auth-browser').hidden = method !== 'browser';
  document.getElementById('auth-file').hidden = method !== 'file';
  form.elements.browser.disabled = method !== 'browser';
  form.elements.cookies.disabled = method !== 'file';
  form.elements.cookies.required = method === 'file';
}

function resetFormState() {
  submit.disabled = false;
  label.textContent = 'Create archive';
  progress.hidden = true;
  form.removeAttribute('aria-busy');
  updateAuthentication();
}

form.addEventListener('change', updateAuthentication);
form.addEventListener('submit', () => {
  submit.disabled = true;
  label.textContent = 'Creating archive…';
  progress.hidden = false;
  form.setAttribute('aria-busy', 'true');
});
window.addEventListener('pageshow', resetFormState);
resetFormState();

for (const retryForm of document.querySelectorAll('.retry-form')) {
  const button = retryForm.querySelector('button');
  const originalLabel = button.textContent;
  retryForm.addEventListener('submit', () => {
    button.disabled = true;
    button.textContent = 'Retrying failed items…';
    retryForm.setAttribute('aria-busy', 'true');
  });
  window.addEventListener('pageshow', () => {
    button.disabled = false;
    button.textContent = originalLabel;
    retryForm.removeAttribute('aria-busy');
  });
}
