import json
import paho.mqtt.client as mqtt

class MQTTClientWrapper:
    def __init__(self, broker: str, port: int, client_id: str = ""):
        self.broker = broker
        self.port = port
        try:
            from paho.mqtt.enums import CallbackAPIVersion
            self.client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id=client_id)
        except (ImportError, AttributeError):
            self.client = mqtt.Client(client_id=client_id)

        self.on_message_callback = None
        self.on_connect_callback = None
        self.on_disconnect_callback = None

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def connect(self):
        self.client.connect(self.broker, self.port, keepalive=60)
        self.client.loop_start()

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()

    def subscribe(self, topic: str):
        self.client.subscribe(topic)

    def unsubscribe(self, topic: str):
        self.client.unsubscribe(topic)

    def publish(self, topic: str, payload: dict, qos: int = 1):
        self.client.publish(topic, json.dumps(payload), qos=qos)

    def _on_connect(self, client, userdata, flags, rc, *args, **kwargs):
        if self.on_connect_callback:
            self.on_connect_callback()

    def _on_disconnect(self, client, userdata, rc, *args, **kwargs):
        if self.on_disconnect_callback:
            self.on_disconnect_callback()

    def _on_message(self, client, userdata, msg):
        if self.on_message_callback:
            try:
                payload = json.loads(msg.payload.decode('utf-8'))
                self.on_message_callback(msg.topic, payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
