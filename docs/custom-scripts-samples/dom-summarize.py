"""Print the JavaScript that summarizes the page's interactive elements.

It lists the visible input, textarea, select, button, a and option elements, and any element
with an inline on* attribute, and gives each a persistent dsid attribute for later reference.
The output is a hierarchy with 2-space indentation showing nesting; elements show only
their dsid (plus type for inputs and value/selected for options), text nodes are quoted
strings, and containers that are not interesting are flattened into their parent.

Usage (script_args is a JSON array of strings):
    dom-summarize.py                          every visible element
    dom-summarize.py onscreen                 only elements inside the viewport
    dom-summarize.py onscreen withpos         also adds pos="x,y,width,height"
"""

import argparse
import json
import sys

TEMPLATE = r'''(function() {
  const FILTER_MODE = "@@FILTER_MODE@@";
  const INCLUDE_POS = "@@INCLUDE_POS@@";
  let dsIdCounter = 1;

  // Find the highest existing dsid to continue numbering from there
  const allElements = document.querySelectorAll('[dsid]');
  allElements.forEach(el => {
    const id = parseInt(el.getAttribute('dsid'));
    if (id >= dsIdCounter) {
      dsIdCounter = id + 1;
    }
  });

  function isElementVisible(element) {
    if (!element) return false;

    const style = window.getComputedStyle(element);

    if (style.display === 'none') return false;
    if (style.visibility === 'hidden') return false;
    if (parseFloat(style.opacity) === 0) return false;

    return true;
  }

  function isElementOnScreen(element) {
    if (!element) return false;

    const rect = element.getBoundingClientRect();
    return (
      rect.top < window.innerHeight &&
      rect.bottom > 0 &&
      rect.left < window.innerWidth &&
      rect.right > 0
    );
  }

  function hasEventListeners(element) {
    for (let attr of element.attributes) {
      if (attr.name.startsWith('on')) {
        return true;
      }
    }
    return false;
  }

  function isInterestingElement(node) {
    if (node.nodeType !== Node.ELEMENT_NODE) return false;
    const tagName = node.tagName.toLowerCase();
    if (tagName === 'input' || tagName === 'textarea' || tagName === 'select' || tagName === 'button' || tagName === 'a' || tagName === 'option') {
      return true;
    }
    if (hasEventListeners(node)) {
      return true;
    }
    return false;
  }

  function traverseDOM(node, depth = 0) {
    const result = [];
    const indent = '  '.repeat(depth);

    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent.trim();
      if (text.length > 0) {
        result.push(`${indent}"${text.substring(0, 100)}${text.length > 100 ? '...' : ''}"`);
      }
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      if (!isElementVisible(node)) {
        return result;
      }

      // Apply onscreen filter if requested
      if (FILTER_MODE === 'onscreen' && !isElementOnScreen(node)) {
        return result;
      }

      const isInteresting = isInterestingElement(node);

      if (isInteresting) {
        // Assign dsid if not already present
        if (!node.hasAttribute('dsid')) {
          node.setAttribute('dsid', dsIdCounter++);
        }
        const dsId = node.getAttribute('dsid');
        const tagName = node.tagName.toLowerCase();

        let elementStr = `${indent}<${tagName} dsid="${dsId}"`;

        // Include type attribute for input elements
        if (tagName === 'input') {
          const type = node.type || 'text';
          elementStr += ` type="${type}"`;
        }

        // Only include value and selected attributes for options
        if (tagName === 'option') {
          if (node.value) elementStr += ` value="${node.value}"`;
          if (node.selected) elementStr += ` selected="true"`;
        }

        // Include position and size if requested
        if (INCLUDE_POS === 'withpos') {
          const rect = node.getBoundingClientRect();
          const x = Math.round(rect.left);
          const y = Math.round(rect.top);
          const width = Math.round(rect.width);
          const height = Math.round(rect.height);
          elementStr += ` pos="${x},${y},${width},${height}"`;
        }

        elementStr += '>';
        result.push(elementStr);

        for (let child of node.childNodes) {
          result.push(...traverseDOM(child, depth + 1));
        }
      } else {
        // Not interesting, flatten: process children at same depth
        for (let child of node.childNodes) {
          result.push(...traverseDOM(child, depth));
        }
      }
    }

    return result;
  }

  function concatenateAdjacentText(lines) {
    const result = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];
      const indent = line.match(/^(\s*)/)[1];
      const isText = line.trim().startsWith('"');

      if (isText) {
        // Collect all adjacent text nodes at the same indentation level
        let concatenated = line.trim().slice(1, -1); // Remove quotes
        let j = i + 1;

        while (j < lines.length) {
          const nextLine = lines[j];
          const nextIndent = nextLine.match(/^(\s*)/)[1];
          const nextIsText = nextLine.trim().startsWith('"');

          if (nextIsText && nextIndent === indent) {
            concatenated += ' ' + nextLine.trim().slice(1, -1);
            j++;
          } else {
            break;
          }
        }

        result.push(`${indent}"${concatenated}"`);
        i = j;
      } else {
        result.push(line);
        i++;
      }
    }

    return result;
  }

  const output = traverseDOM(document.body);
  const concatenated = concatenateAdjacentText(output);
  return concatenated.join('\n');
})();
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("filter_mode", nargs="?", default="all", choices=["all", "onscreen"])
    parser.add_argument("include_pos", nargs="?", default="", choices=["", "withpos"])
    args = parser.parse_args()
    if args.include_pos == "withpos" and args.filter_mode != "onscreen":
        parser.error("'withpos' option requires 'onscreen' mode")
    js = (TEMPLATE.replace('"@@FILTER_MODE@@"', json.dumps(args.filter_mode))
                  .replace('"@@INCLUDE_POS@@"', json.dumps(args.include_pos)))
    sys.stdout.buffer.write(js.encode("utf-8"))


if __name__ == "__main__":
    main()
