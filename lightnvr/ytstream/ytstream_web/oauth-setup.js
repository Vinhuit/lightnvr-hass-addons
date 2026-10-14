async function showOAuthSetup(response) {
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Cannot save OAuth client');
    document.getElementById('oauth-client-status').textContent = result.configured
        ? 'OAuth client saved. Choose Authorize to connect YouTube.'
        : 'Upload a Google OAuth Web application client to connect YouTube.';
    document.getElementById('oauth-redirect-uri').textContent = result.redirect_uri;
}
fetch('api/oauth/client').then(showOAuthSetup).catch(error => {
    document.getElementById('oauth-client-status').textContent = error.message;
});
document.getElementById('oauth-client-file').addEventListener('change', async event => {
    const file = event.target.files[0];
    if (!file) return;
    try {
        const client = JSON.parse(await file.text());
        await showOAuthSetup(await fetch('api/oauth/client', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(client)
        }));
    } catch (error) {
        document.getElementById('oauth-client-status').textContent = error.message;
    } finally {
        event.target.value = '';
    }
});
