'use strict';
const uploadForm = document.getElementById('upload-form');
uploadForm?.addEventListener('submit',()=>{
  const button = document.getElementById('upload-button');
  button.disabled = true;
  button.textContent = 'Importing…';
  document.getElementById('upload-status').textContent = 'Uploading and checking the export. Please keep this page open until the import finishes.';
});
