"""Print the JavaScript that extracts one day's events from Google Calendar's month view.

Usage (script_args is a JSON array of strings):
    gcal-daily-events-js.py 8                8th of the current month and year
    gcal-daily-events-js.py 15 10            October 15 of the current year
    gcal-daily-events-js.py 25 12 2024       December 25, 2024
"""

import argparse
import datetime
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
   * Extract events for a specific date from Google Calendar monthly view
   * @param {number} targetDay - Day of the month (1-31)
   * @param {number} targetMonth - Month (1-12)
   * @param {number} targetYear - Year (e.g., 2025)
   * @returns {Promise<Object>} Events for the specified date
   */
  function getDailyEvents(targetDay, targetMonth, targetYear) {
    try {
      console.log(`Extracting events for ${targetMonth}/${targetDay}/${targetYear}`);
      
      // Calculate container index for the target date
      const firstDay = new Date(targetYear, targetMonth - 1, 1);
      const firstDayOfWeek = (firstDay.getDay() - getWeekStart() + 7) % 7; // grid column of the 1st (0 = first column)
      const containerIndex = firstDayOfWeek + targetDay - 1;
      
      console.log(`First day of month offset: ${firstDayOfWeek}`);
      console.log(`Target container index: ${containerIndex}`);
      
      // Find all day containers
      const containers = document.querySelectorAll('.qLWd9c');
      console.log(`Total containers found: ${containers.length}`);
      
      if (containerIndex < 0 || containerIndex >= containers.length) {
        return Promise.resolve({
          success: false,
          error: `Invalid date or container index ${containerIndex} out of range (0-${containers.length-1})`,
          date: `${targetYear}-${targetMonth.toString().padStart(2, '0')}-${targetDay.toString().padStart(2, '0')}`,
          containerIndex: containerIndex,
          totalContainers: containers.length
        });
      }
      
      const dayContainer = containers[containerIndex];
      const dayEvents = [];
      
      // Extract all text content from the day container
      const allElements = dayContainer.querySelectorAll('*');
      const processedTexts = new Set(); // Avoid duplicates
      
      console.log(`Found ${allElements.length} elements in day container`);
      
      for (let element of allElements) {
        const text = element.textContent?.trim();
        
        if (text && 
            (text.includes('am') || text.includes('pm') || 
             text.includes('meeting') || text.includes('standup') || 
             text.includes('1:1') || text.includes(':')) &&
            text.length > 2 && text.length < 200 &&
            !processedTexts.has(text)) {
          
          processedTexts.add(text);
          
          // Try to parse time information
          const timeMatch = text.match(/(\d{1,2}(?::\d{2})?\s*(am|pm))/gi);
          const hasTimeInfo = timeMatch !== null;
          
          // Extract event title (remove time information)
          let eventTitle = text;
          if (timeMatch) {
            timeMatch.forEach(time => {
              eventTitle = eventTitle.replace(time, '').trim();
            });
          }
          
          // Clean up common suffixes/prefixes
          eventTitle = eventTitle
            .replace(/^[-\s•]+|[-\s•]+$/g, '') // Remove leading/trailing dashes, spaces, bullets
            .replace(/\s+/g, ' ') // Normalize spaces
            .trim();
          
          if (eventTitle && eventTitle.length > 0) {
            const eventInfo = {
              title: eventTitle,
              rawText: text,
              times: timeMatch || [],
              hasTimeInfo: hasTimeInfo,
              element: {
                tagName: element.tagName,
                className: element.className,
                hasClickableClass: element.className.includes('WBi6vc') || element.className.includes('nHqeVd')
              }
            };
            
            // Check for status indicators
            if (text.includes('Needs RSVP')) eventInfo.rsvpStatus = 'needs_rsvp';
            else if (text.includes('Accepted')) eventInfo.rsvpStatus = 'accepted';
            else if (text.includes('Tentative')) eventInfo.rsvpStatus = 'tentative';
            else if (text.includes('event_busy')) eventInfo.status = 'busy';
            
            // Check for meeting indicators
            if (text.toLowerCase().includes('zoom') || text.toLowerCase().includes('meet')) {
              eventInfo.meetingType = 'video_call';
            }
            
            // Extract location if present
            const locationMatch = text.match(/Location:\s*([^,]+)/);
            if (locationMatch) {
              eventInfo.location = locationMatch[1].trim();
            }
            
            dayEvents.push(eventInfo);
          }
        }
      }
      
      // Sort events by time if available
      dayEvents.sort((a, b) => {
        if (a.times.length > 0 && b.times.length > 0) {
          const timeA = a.times[0].toLowerCase();
          const timeB = b.times[0].toLowerCase();
          
          // Convert to 24-hour format for comparison
          const parseTime = (timeStr) => {
            const match = timeStr.match(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)/i);
            if (match) {
              let hours = parseInt(match[1]);
              const minutes = match[2] ? parseInt(match[2]) : 0;
              const isPM = match[3].toLowerCase() === 'pm';
              
              if (isPM && hours !== 12) hours += 12;
              if (!isPM && hours === 12) hours = 0;
              
              return hours * 60 + minutes;
            }
            return 0;
          };
          
          return parseTime(timeA) - parseTime(timeB);
        }
        
        // If no times, sort by title
        return a.title.localeCompare(b.title);
      });
      
      // Create response object
      const response = {
        success: true,
        date: `${targetYear}-${targetMonth.toString().padStart(2, '0')}-${targetDay.toString().padStart(2, '0')}`,
        dayOfWeek: new Date(targetYear, targetMonth - 1, targetDay).getDay(),
        dayName: new Date(targetYear, targetMonth - 1, targetDay).toLocaleDateString('en-US', { weekday: 'long' }),
        monthName: new Date(targetYear, targetMonth - 1, targetDay).toLocaleDateString('en-US', { month: 'long' }),
        containerIndex: containerIndex,
        eventCount: dayEvents.length,
        events: dayEvents,
        extractedAt: new Date().toISOString(),
        message: `Successfully extracted ${dayEvents.length} events for ${targetMonth}/${targetDay}/${targetYear}`
      };
      
      console.log(`Extracted ${dayEvents.length} events for the day`);
      
      return Promise.resolve(response);
      
    } catch (error) {
      return Promise.resolve({
        success: false,
        error: `Error extracting daily events: ${error.message}`,
        date: `${targetYear}-${targetMonth.toString().padStart(2, '0')}-${targetDay.toString().padStart(2, '0')}`,
        data: null
      });
    }
  }
  
  // Execute the function with provided parameters
  return getDailyEvents(@@DAY@@, @@MONTH@@, @@YEAR@@);
})();
'''


def main() -> None:
    today = datetime.date.today()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("day", type=int)
    parser.add_argument("month", type=int, nargs="?", default=today.month)
    parser.add_argument("year", type=int, nargs="?", default=today.year)
    args = parser.parse_args()
    if not 1 <= args.day <= 31 or not 1 <= args.month <= 12:
        parser.error("day must be 1-31 and month 1-12")
    # Integers only, so nothing a caller passes can become JavaScript.
    js = (TEMPLATE.replace("@@DAY@@", str(args.day))
                  .replace("@@MONTH@@", str(args.month))
                  .replace("@@YEAR@@", str(args.year)))
    sys.stdout.buffer.write(js.encode("utf-8"))


if __name__ == "__main__":
    main()
