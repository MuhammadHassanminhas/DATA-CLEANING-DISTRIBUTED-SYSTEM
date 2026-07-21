# Protocol Versioning Strategy

## Purpose

This document defines how the communication protocol between the Coordinator and Workers evolves over time.

The goal is to allow future versions of the system to introduce new features while maintaining compatibility whenever possible.

---

# Current Version

Protocol Version: 1.0

Message Version: 1.0

API Version: v1

These values represent the first stable communication protocol.

---

# Compatibility Rules

The following rules apply to all future protocol changes.

## 1. Breaking Changes

Breaking changes require a new protocol version.

Examples:

- Removing a required field
- Renaming a message
- Changing the meaning of an existing field
- Removing an endpoint

---

## 2. Non-Breaking Changes

The following changes are considered compatible:

- Adding optional fields
- Adding new message types
- Adding new endpoints
- Adding new enum values (when older services can safely ignore them)

---

## 3. Coordinator Responsibility

The Coordinator is responsible for validating the protocol version presented by a Worker.

If an unsupported version is detected, the Coordinator must reject the connection.

---

## 4. Worker Responsibility

A Worker must always identify the protocol version it supports during registration and authentication.

---

## 5. Future Strategy

Future protocol versions should strive for backward compatibility whenever practical.

Older workers should continue functioning if the Coordinator still supports their protocol version.

---

# Guiding Principle

Never change the protocol silently.

Every incompatible change must be reflected by a new protocol version.