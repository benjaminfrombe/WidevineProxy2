async function processMessage(detail) {
    try {
        // This will throw if extension context is invalidated
        const _ = chrome.runtime.id;
    } catch (error) {
        return detail.body;
    }
    
    return new Promise((resolve) => {
        try {
            chrome.runtime.sendMessage({
                type: detail.type,
                body: detail.body,
            }, (response) => {
                if (chrome.runtime.lastError) {
                    return resolve(detail.body);
                }
                resolve(response);
            });
        } catch (error) {
            resolve(detail.body);
        }
    });
}

document.addEventListener('response', async (event) => {
    const { detail } = event;
    let responseData = detail.body;

    try {
        responseData = await processMessage(detail);
    } catch (error) {
        responseData = detail.body;
    }

    const responseEvent = new CustomEvent('responseReceived', {
        detail: detail.requestId.concat(responseData)
    });
    document.dispatchEvent(responseEvent);
});
