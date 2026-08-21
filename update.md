# Teamatrix v3.0 Feature Update Roadmap

This document outlines the planned UI/UX and backend logic improvements to make the system more user-friendly, professional, and robust.

## 1. Inventory & Container Management (New Tab)
* **Inventory Tracking Tab:** Create a dedicated tab alongside "Dashboard" and "Station Mgr" to manage raw materials.
* **Refill Logging:** Include an input field to manually enter the weight of tea added to a container. The system will log the exact date, time, and weight of every refill action.
* **Live Deduction Logic:** The system must automatically deduct the *actual dispensed weight* (from the scale) from the virtual container inventory after every run.
* **Low Stock Warnings:** Implement a visual warning (e.g., yellow alert/icon) on the dashboard when a container's remaining weight drops to or below **100g**.
* **Insufficient Stock Block:** Update the pre-dispense logic. If an order requires more tea than what is available in the container (e.g., Order needs 100g, container has 70g), the system must block the order and throw a clear error to the user before starting the sequence.

## 2. Order Queue Enhancements
* **Card-Based UI:** Upgrade the "Order Queue" section from a simple text list to a modern Card View, making it easier to read at a glance.
* **Order IDs:** Display a unique, auto-generated Order ID on every card for easy tracking.
* **Queue Management (CRUD):** Add interactive buttons to each queue card allowing the user to **Edit** (change weight/recipe), **Update**, or **Delete** the order before it is processed.

## 3. Recipe & Menu Management
* **Base Tea Integration:** Add the 5 core Base Teas to the recipe selection menu. Allow customers to order *only* a base tea without any additional ingredients.
* **Accessible Recipe Editor:** Create a dedicated UI (modal or tab) to easily manage recipes.
* **Dynamic Editing:** Users must be able to change recipe names, add/remove ingredients, and adjust the ratios/percentages on the fly without altering the source code.

## 4. Hardware Sequence & Control Logic (Conveyor & Mixer)
* **Automated Sequencing:** Update the hardware logic so that:
    1.  The **Conveyor** automatically starts as soon as the first ingredient begins dispensing.
    2.  The **Mixer** is delayed and only activates *after* all ingredients for that specific recipe (e.g., all 3 stations) have successfully finished dispensing.
* **Manual Overrides:** Add dedicated, clearly labeled "Manual Start" and "Manual Stop" buttons for both the Conveyor and the Mixer on the global dashboard, allowing operators to clear jams or test hardware.

## 5. System Stability & Error Handling
* **Improved Error UI:** Move away from silent failures or console-only logs. Implement a centralized, highly visible notification system (e.g., toast notifications or a dedicated alert banner) for user-facing errors.
* **Clear Messaging:** Ensure all errors (e.g., "Station 3 Disconnected", "Insufficient Stock in Station 1", "Scale 2 Timeout") are written in plain, actionable language.


A Quick Tip on Implementation
Since you are tracking live inventory vs. required inventory (Points 3 & 4), make sure you check the stock for all ingredients in a recipe before you drop the first ingredient. You don't want a scenario where the system drops the first two ingredients perfectly, but then realizes it doesn't have enough of the third ingredient, ruining the batch on the conveyor!