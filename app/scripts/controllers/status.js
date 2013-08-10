GLClient.controller('StatusCtrl',
  ['$scope', '$rootScope', '$routeParams', 'Tip', '$cookies', 'Contexts',
  function($scope, $rootScope, $routeParams, Tip, $cookies, Contexts) {
    $scope.tip_id = $routeParams.tip_id;

    $rootScope.fileUploader = {};
    $rootScope.fileUploader.uploadedFiles = [];
    $rootScope.fileUploader.uploadingFiles = [];

    var TipID = {tip_id: $scope.tip_id};
    new Tip(TipID, function(tip){

      Contexts.query(function(contexts){
        $scope.tip = tip;
        $scope.contexts = contexts;
        $scope.fieldFormat = {};

        angular.forEach(contexts, function(context, k){
          if (context.context_gus == $scope.tip.context_gus) {
            $scope.current_context = context;
          }
        });
        angular.forEach($scope.current_context.fields,
                        function(field){
          $scope.fieldFormat[field.key] = field; 
        });

      });
    });

    $scope.getField = function(field_name) {
      angular.forEach($scope.current_context.fields,
                      function(field){
        if ( field.key  == field_name ) {
          console.log(field);
          return field; 
        }
      });
    };

    $scope.newComment = function() {
      $scope.tip.comments.newComment($scope.newCommentContent);
      $scope.newCommentContent = '';
    };

    if ($cookies['role'] === 'wb') {
      $rootScope.whistleblower_tip_id = $cookies['tip_id'];
    }

}]);

GLClient.controller('FileDetailsCtrl', ['$scope', function($scope){
    $scope.securityCheckOpen = false;

    $scope.openSecurityCheck = function() {
      $scope.securityCheckOpen = true;
    };

    $scope.closeSecurityCheck = function() {
      $scope.securityCheckOpen = false;
    };

    $scope.securityCheckOptions = {
      backdropFade: true,
      dialogFade: true
    }
}]);
