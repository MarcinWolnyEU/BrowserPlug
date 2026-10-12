"""Print the JavaScript that opens one Google Calendar event and reads its details.

The event is found in the current month's view on the given day, by exact title or else by
text that contains the title and the time. The month on screen must be the current one.

Usage (script_args is a JSON array of strings):
    gcal-cal-event-js.py "Team Lunch" 8 "11:30am"
"""

import argparse
import json
import sys

TEMPLATE = r'''(function() {
  // The month grid starts on the user's first day of the week (Monday in most of Europe),
  // not always on Sunday. Read it from the first column header: returns 0 for Sunday .. 6.
  function getWeekStart() {
    const header = document.querySelector('[role="columnheader"]');
    const text = (header ? header.textContent : '').trim().toLowerCase();
    if (!text) return 0;
    for (const lang of [document.documentElement.lang, 'en-US']) {
      if (!lang) continue;
      try {
        for (let d = 0; d < 7; d++) {
          // 1 January 2023 was a Sunday
          const name = new Date(2023, 0, 1 + d).toLocaleDateString(lang, { weekday: 'long' }).toLowerCase();
          if (text.startsWith(name)) return d;
        }
      } catch (e) { /* unusable language tag: try the next */ }
    }
    return 0; // unrecognised header: assume Sunday
  }

  /**
   * Get detailed information about a specific calendar event
   * @param {string} eventTitle - The title of the event to find
   * @param {number} day - The day of the month (1-31)
   * @param {string} time - Time string to match (e.g., "11:30am", "2:00pm")
   * @returns {Promise<Object>} Event details as JSON or error
   */
  function getEventDetails(eventTitle, day, time) {
    try {
      // Calculate container index for the given day
      const today = new Date();
      const currentMonth = today.getMonth();
      const currentYear = today.getFullYear();
      
      const firstDay = new Date(currentYear, currentMonth, 1);
      const firstDayOfWeek = (firstDay.getDay() - getWeekStart() + 7) % 7; // grid column of the 1st (0 = first column)
      
      // Container index = first day offset + target day - 1
      const containerIndex = firstDayOfWeek + day - 1;
      
      console.log('Searching for event:', eventTitle, 'on day', day, 'at', time);
      console.log('Container index calculated:', containerIndex);
      
      // Find day containers using knowledge base method
      const containers = document.querySelectorAll('.qLWd9c');
      console.log('Total containers found:', containers.length);
      
      if (containerIndex >= 0 && containerIndex < containers.length) {
        const targetContainer = containers[containerIndex];
        console.log('Target container found for day', day);
        
        // Be more specific about which element to click
        const allElements = document.querySelectorAll('*');
        let matchedEvent = null;
        
        // First try: Look for exact title match in SPAN elements
        for (let el of allElements) {
          const text = el.textContent?.trim();
          if (text === eventTitle && el.tagName === 'SPAN' && 
              (el.className.includes('WBi6vc') || el.className.includes('nHqeVd'))) {
            // Verify this element is within our target day container
            if (targetContainer.contains(el)) {
              console.log('Found exact event title span:', text, 'className:', el.className);
              matchedEvent = el;
              break;
            }
          }
        }
        
        // Second try: Look for event elements with both title and time
        if (!matchedEvent) {
          for (let el of allElements) {
            const text = el.textContent?.trim();
            if (text && text.includes(eventTitle) && text.includes(time) && 
                targetContainer.contains(el) &&
                (el.tagName !== 'DIV' || text.length < 200)) {
              console.log('Found event with title and time:', text.substring(0, 50));
              matchedEvent = el;
              break;
            }
          }
        }
        
        if (matchedEvent) {
          console.log('Clicking on matched event element:', matchedEvent.tagName, matchedEvent.className);
          
          // Click on the event to open details dialog
          matchedEvent.click();
          
          // Return a promise that resolves with the dialog content
          return new Promise((resolve) => {
            setTimeout(() => {
              const dialog = document.querySelector('[role="dialog"]');
              
              if (dialog) {
                const dialogText = dialog.textContent;
                
                // Check if this is the event details dialog (not "add event" dialog)
                if (dialogText.includes(eventTitle) && 
                    (dialogText.includes('Edit event') || dialogText.includes('Close') || dialogText.includes('Options'))) {
                  
                  // Look for the specific xDetDlg element
                  const xDetDlgElement = document.getElementById('xDetDlg') || 
                                        document.querySelector('.xDetDlg') ||
                                        dialog.querySelector('[id*="xDetDlg"]') ||
                                        dialog.querySelector('[class*="xDetDlg"]');
                  
                  let contentElement = xDetDlgElement || dialog;
                  
                  // Parse the content into JSON structure
                  const eventData = {
                    title: null,
                    date: null,
                    time: null,
                    recurrence: null,
                    meeting: {
                      type: null,
                      joinUrl: null,
                      meetingId: null,
                      passcode: null,
                      phoneNumbers: [],
                      host: null
                    },
                    attendees: {
                      total: null,
                      accepted: null,
                      awaiting: null,
                      organizer: null,
                      guests: []
                    },
                    description: null,
                    attachments: [],
                    notifications: [],
                    location: null,
                    rsvpStatus: null
                  };
                  
                  // Extract title
                  const titleEl = contentElement.querySelector('#rAECCd') || 
                                 contentElement.querySelector('[role="heading"]');
                  if (titleEl) {
                    eventData.title = titleEl.textContent?.trim();
                  }
                  
                  // Extract date and time
                  const dateTimeEl = contentElement.querySelector('.AzuXid.O2VjS.CyPPBf');
                  if (dateTimeEl) {
                    const fullText = dateTimeEl.textContent;
                    const parts = fullText.split('⋅');
                    if (parts.length >= 2) {
                      eventData.date = parts[0]?.trim();
                      eventData.time = parts[1]?.trim();
                    }
                  }
                  
                  // Extract recurrence
                  const recurrenceEl = contentElement.querySelector('.AzuXid.Kcwcnf.CyPPBf');
                  if (recurrenceEl) {
                    eventData.recurrence = recurrenceEl.textContent?.trim();
                  }
                  
                  // Extract Zoom meeting info
                  const zoomLink = contentElement.querySelector('a[href*="zoom.us"]');
                  if (zoomLink) {
                    eventData.meeting.type = 'Zoom';
                    eventData.meeting.joinUrl = zoomLink.href;
                    
                    // Extract meeting ID and passcode
                    const idMatch = dialogText.match(/ID:\s*(\d+)/);
                    if (idMatch) eventData.meeting.meetingId = idMatch[1];
                    
                    const passcodeMatch = dialogText.match(/Passcode:\s*(\d+)/);
                    if (passcodeMatch) eventData.meeting.passcode = passcodeMatch[1];
                  }
                  
                  // Extract phone numbers
                  const phoneLinks = contentElement.querySelectorAll('a[href^="tel:"]');
                  phoneLinks.forEach(phoneLink => {
                    const phoneText = phoneLink.textContent;
                    const regionMatch = phoneText.match(/\((\w+)\)/);
                    const numberMatch = phoneText.match(/([+\d\s-]+)/);
                    if (regionMatch && numberMatch) {
                      eventData.meeting.phoneNumbers.push({
                        region: regionMatch[1],
                        number: numberMatch[1].trim(),
                        passcode: eventData.meeting.passcode
                      });
                    }
                  });
                  
                  // Extract meeting host
                  const hostMatch = dialogText.match(/Meeting host:\s*([^\s]+@[^\s]+)/);
                  if (hostMatch) {
                    eventData.meeting.host = hostMatch[1];
                  }
                  
                  // Extract attendee info
                  const guestMatch = dialogText.match(/(\d+)\s+guests/);
                  if (guestMatch) eventData.attendees.total = parseInt(guestMatch[1]);
                  
                  const rsvpMatch = dialogText.match(/(\d+)\s+yes\s*(\d+)\s+awaiting/);
                  if (rsvpMatch) {
                    eventData.attendees.accepted = parseInt(rsvpMatch[1]);
                    eventData.attendees.awaiting = parseInt(rsvpMatch[2]);
                  }
                  
                  // Extract organizer
                  const organizerMatch = dialogText.match(/Organizer:\s*([^\n]+)/);
                  if (organizerMatch) {
                    eventData.attendees.organizer = organizerMatch[1].trim();
                  }
                  
                  // Extract description
                  const descEl = contentElement.querySelector('#xDetDlgDesc');
                  if (descEl) {
                    const descLink = descEl.querySelector('a');
                    if (descLink) {
                      eventData.description = {
                        text: descLink.textContent?.trim(),
                        url: descLink.href
                      };
                    } else {
                      eventData.description = descEl.textContent?.replace('Description:', '').trim();
                    }
                  }
                  
                  // Extract attachments
                  const attachmentLinks = contentElement.querySelectorAll('#xDetDlgAtm a');
                  attachmentLinks.forEach(link => {
                    eventData.attachments.push({
                      name: link.textContent?.trim(),
                      url: link.href
                    });
                  });
                  
                  // Extract notifications
                  const notificationEl = contentElement.querySelector('#xDetDlgNot');
                  if (notificationEl) {
                    const notifications = notificationEl.querySelectorAll('li');
                    notifications.forEach(notif => {
                      eventData.notifications.push(notif.textContent?.trim());
                    });
                  }
                  
                  // Close the dialog
                  const closeButton = dialog.querySelector('[aria-label="Close"]');
                  if (closeButton) {
                    closeButton.click();
                  }
                  
                  resolve({
                    success: true,
                    eventData: eventData,
                    message: 'Event details extracted successfully as JSON'
                  });
                } else if (dialogText.includes('Create') || dialogText.includes('Add event')) {
                  // We accidentally opened "add event" dialog, close it
                  const closeButton = dialog.querySelector('[aria-label="Close"]');
                  if (closeButton) {
                    closeButton.click();
                  }
                  
                  resolve({
                    success: false,
                    error: 'Clicked wrong element - opened "add event" dialog instead of event details',
                    dialogType: 'create_event'
                  });
                } else {
                  resolve({
                    success: false,
                    error: 'Opened dialog but could not identify type',
                    dialogContent: dialogText.substring(0, 100)
                  });
                }
              } else {
                resolve({
                  success: false,
                  error: 'No dialog appeared after clicking event'
                });
              }
            }, 1500);
          });
        } else {
          return Promise.resolve({
            success: false,
            error: `Event "${eventTitle}" not found on day ${day} at ${time}`,
            containerIndex: containerIndex,
            totalContainers: containers.length
          });
        }
      } else {
        return Promise.resolve({
          success: false,
          error: `Invalid day: ${day} (container index ${containerIndex} out of range)`,
          containerIndex: containerIndex,
          totalContainers: containers.length
        });
      }
    } catch (error) {
      return Promise.resolve({
        success: false,
        error: `Error: ${error.message}`
      });
    }
  }
  
  // Execute the function with provided parameters
  return getEventDetails("@@TITLE@@", @@DAY@@, "@@TIME@@");
})();
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("title", help="exact event title")
    parser.add_argument("day", type=int, help="day of the current month, 1-31")
    parser.add_argument("time", help='time text to match, e.g. "11:30am"')
    args = parser.parse_args()
    if not 1 <= args.day <= 31:
        parser.error("day must be 1-31")
    # The strings go in as JSON literals (valid JavaScript), so a quote or a backslash in
    # a title is data. The day is an integer.
    js = (TEMPLATE.replace('"@@TITLE@@"', json.dumps(args.title))
                  .replace("@@DAY@@", str(args.day))
                  .replace('"@@TIME@@"', json.dumps(args.time)))
    sys.stdout.buffer.write(js.encode("utf-8"))


if __name__ == "__main__":
    main()
