#!/usr/bin/env python
# vim: noet

import json

class Config():
	def __init__(self, path):
		
		# load the config, and parse it. i chose json because
		# it's in the python stdlib and is language-neutral
		with open(path) as f:
			self.raw = f.read()
			self.data = json.loads(self.raw)

	def __getitem__(self, items):
		return self.data[items]