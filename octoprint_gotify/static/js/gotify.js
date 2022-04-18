$(function() {
    function GotifyViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[1];

        self.testActive = ko.observable(false);
        self.testResult = ko.observable(false);
        self.testSuccessful = ko.observable(false);
        self.testMessage = ko.observable();

        self.testNotification  = function() {
            self.testActive(true);
            self.testResult(false);
            self.testSuccessful(false);
            self.testMessage("");

            var app_token = $('#app_token').val();
            var userkey = $('#userkey').val();
            var device = $('#device').val();
            var image = $('#image').is(':checked');

            var payload = {
                command: "test",
                app_token: app_token,
                device: device,
                image: image,
            };

            $.ajax({
                url: API_BASEURL + "plugin/gotify",
                type: "POST",
                dataType: "json",
                data: JSON.stringify(payload),
                contentType: "application/json; charset=UTF-8",
                success: function(response) {
                    self.testResult(true);
                    self.testSuccessful(response.success);
                    if (!response.success && response.hasOwnProperty("msg")) {
                        self.testMessage(response.msg);
                    } else {
                        self.testMessage(undefined);
                    }
                },
                complete: function() {
                    self.testActive(false);
                }
            });
        };

        self.onBeforeBinding = function() {
            self.settings = self.settingsViewModel.settings;
        };

        self.has_own_token = function() {
            return self.settings.plugins.gotify.token() != '' && self.settings.plugins.gotify.token() != self.settings.plugins.gotify.default_token();
        };

    }

    // view model class, parameters for constructor, container to bind to
    ADDITIONAL_VIEWMODELS.push([GotifyViewModel, ["loginStateViewModel", "settingsViewModel"], document.getElementById("settings_plugin_gotify")]);
});
