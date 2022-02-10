# Gatekeeper

This application is responsible for opening and closing the front gate.

## Context

Gatekeeper is a raspberry pi (hostname `zuul`) that is responsible for opening the front gate for guest and members. The pi is used to control the gate remote control and uses a Sim800 module to receive calls.

## Usage

The gatekeeper application uses a whitelist in the following format:

    +123456 owner

The first element is the phone number in international notation without spaces or other punctuation, the second element is a free text field describing the owner of the phone number.

The application can be deployed using the included systemd service.

To run the application without systemd, use the following command:

    python3 main.py -d ./whitelist -v --no-journald -m mqtt


## Special operation modes

Gatekeeper is aware of two special operation modes, event mode (i.e. for Newline or other events) and the Thursday social evening.

### Thursday social

The gate should open when any number calls.

### Event mode

Sometimes it's useful to be able to manually override the CID check; eg during newline or other events. Event mode is activated by creating the `/tmp/eventmode` file; eg using `touch /tmp/eventmode`. This path has been chosen since it doesn't persist. This way, someone unfamiliar with the system can just turn it off and on again to restore it back to the standard operational state. 

## MQTT

Gatekeeper can also be monitored and controlled using MQTT.

Gatekeeper listens to commands on the `hsg/gatekeeper/cmd` topic. So far it supports the following commands:

* `open`: open the gate
* `eventmode?`: query event mode state, result will be published in the `hsg/gatekeeper/eventmode` topic.

Gatekeeper will also publish events on the `hsg/gatekeeper/*` topics:

* `hsg/gatekeeper/open`: this topic will have a message published with the owner as described in the data file upon a successful call to open the gate.
* `hsg/gatekeeper/ring`: this topic will have a message published when the phone number of the Gatekeeper device is rang. This does not signal a successful authentication. The contents of the messages in this topic should be discarded.
* `hsg/gatekeeper/eventmode`: this topic will disclose if event mode is enabled or not. When a 1 is published, event mode is enabled. 0 means disabled.


